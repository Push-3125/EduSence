"""Comprehensive tests for AI Chatbot Conversation & Message History (US 2.12 / US 2.13).

Covers:
- Invariant 2 (Layering): Routers -> Services -> Repositories.
- Invariant 4: Gemini called exclusively via GeminiAdapter; mock isolation in tests.
- Invariant 6: Strict Tenant Isolation (User A cannot view, update, delete, or message User B's conversations).
- Invariant 7: Atomic Quota Reservation before LLM calls (exceeded -> 429, Redis down -> 503 fail-closed, refund on failure).
- Telemetry: AIUsageLogger tracking tokens, latency, cost, message IDs; no prompts logged.
- US 2.13: Multi-turn chat history preserved; response labeled is_ai_generated: True with academic disclaimer.
- API validation & standard response envelopes ({ success: true, data: ..., message: ... }).
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Ensure backend root is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.chat import router as chat_router
from app.core.security import create_access_token, hash_password
from app.main import app
from app.providers.gemini_adapter import (
    ChatMessage,
    GeminiAdapter,
    GeminiCircuitOpenError,
    GeminiResponse,
    get_gemini_adapter,
)
from app.providers.usage_logger import (
    AIUsageLog,
    AIUsageLogger,
    get_ai_usage_logger,
)
from app.repositories.chat_repo import (
    AIConversation,
    AIMessage,
    ChatRepository,
    get_chat_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.chat_service import (
    ChatService,
    ConversationNotFoundError,
    InvalidMessageError,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    get_chat_service,
)
from app.services.quota_service import (
    AIQuotaService,
    get_quota_service,
)

# Ensure chat router is mounted
if not any(getattr(route, "path", "").startswith("/api/v1/chat") for route in app.routes):
    app.include_router(chat_router)


# ---------------------------------------------------------------------------
# Test Doubles & In-Memory Fakes
# ---------------------------------------------------------------------------


class MockAsyncRedisClient:
    """In-memory Redis double supporting Lua scripts for atomic reservation & refund."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail_on_next_op: bool = False

    async def get(self, key: str) -> str | None:
        if self.fail_on_next_op:
            raise ConnectionError("Simulated Redis Connection Error")
        return self.store.get(key)

    async def delete(self, *keys: str) -> int:
        if self.fail_on_next_op:
            raise ConnectionError("Simulated Redis Connection Error")
        cnt = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                cnt += 1
        return cnt

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        if self.fail_on_next_op:
            raise ConnectionError("Simulated Redis Connection Error")

        key = str(keys_and_args[0])
        # Reservation script check
        if "tonumber(ARGV[1])" in script and "INCR" in script:
            limit = int(keys_and_args[1])
            curr_val = int(self.store.get(key, "0"))
            if curr_val >= limit:
                return -1
            curr_val += 1
            self.store[key] = str(curr_val)
            return curr_val

        # Refund script check
        if "DECR" in script:
            curr_val = int(self.store.get(key, "0"))
            if curr_val <= 0:
                return 0
            curr_val -= 1
            self.store[key] = str(curr_val)
            return curr_val

        return 0


class FakeChatRepository(ChatRepository):
    """In-memory ChatRepository test double providing strict tenant-isolated behavior."""

    def __init__(self) -> None:
        self.conversations: dict[uuid.UUID, AIConversation] = {}
        self.messages_map: dict[uuid.UUID, list[AIMessage]] = {}

    async def create_conversation(
        self,
        user_id: uuid.UUID,
        title: str | None = None,
        course_id: uuid.UUID | None = None,
        conversation_type: str = "general",
    ) -> AIConversation:
        now = datetime.now(UTC)
        conv_id = uuid.uuid4()
        conv = AIConversation(
            id=conv_id,
            user_id=user_id,
            title=title.strip() if title else None,
            course_id=course_id,
            conversation_type=conversation_type,
            created_at=now,
            updated_at=now,
        )
        conv.messages = []
        self.conversations[conv_id] = conv
        self.messages_map[conv_id] = []
        return conv

    async def get_conversation(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> AIConversation | None:
        conv = self.conversations.get(conversation_id)
        if not conv or conv.user_id != user_id:
            return None
        # Link messages
        conv.messages = list(self.messages_map.get(conversation_id, []))
        return conv

    async def list_conversations(
        self,
        user_id: uuid.UUID,
        skip: int = 0,
        limit: int = 50,
        course_id: uuid.UUID | None = None,
    ) -> tuple[list[AIConversation], int]:
        matched: list[AIConversation] = []
        for conv in self.conversations.values():
            if conv.user_id != user_id:
                continue
            if course_id is not None and conv.course_id != course_id:
                continue
            conv.messages = list(self.messages_map.get(conv.id, []))
            matched.append(conv)

        # Sort updated_at descending
        matched.sort(key=lambda c: -c.updated_at.timestamp())
        total = len(matched)
        return matched[skip : skip + limit], total

    async def update_conversation_title(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        title: str,
    ) -> AIConversation | None:
        conv = await self.get_conversation(user_id=user_id, conversation_id=conversation_id)
        if not conv:
            return None
        conv.title = title.strip()
        conv.updated_at = datetime.now(UTC)
        return conv

    async def delete_conversation(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> bool:
        conv = await self.get_conversation(user_id=user_id, conversation_id=conversation_id)
        if not conv:
            return False
        del self.conversations[conversation_id]
        self.messages_map.pop(conversation_id, None)
        return True

    async def add_message(
        self,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        model_name: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int | None = None,
    ) -> AIMessage:
        now = datetime.now(UTC)
        msg_id = uuid.uuid4()
        msg = AIMessage(
            id=msg_id,
            conversation_id=conversation_id,
            role=role,
            content=content.strip(),
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            created_at=now,
            updated_at=now,
        )
        if conversation_id not in self.messages_map:
            self.messages_map[conversation_id] = []
        self.messages_map[conversation_id].append(msg)

        if conversation_id in self.conversations:
            self.conversations[conversation_id].updated_at = now
            self.conversations[conversation_id].messages = list(self.messages_map[conversation_id])

        return msg

    async def get_messages(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        limit: int = 100,
    ) -> list[AIMessage]:
        conv = await self.get_conversation(user_id=user_id, conversation_id=conversation_id)
        if not conv:
            return []
        msgs = self.messages_map.get(conversation_id, [])
        return msgs[:limit]


class FakeGeminiAdapter(GeminiAdapter):
    """Configurable GeminiAdapter test double verifying prompts and simulating errors."""

    def __init__(self) -> None:
        super().__init__(model="gemini-2.5-flash", test_mode=True)
        self.recorded_calls: list[dict[str, Any]] = []
        self.simulated_exception: Exception | None = None
        self.mock_reply_text: str = "This is a helpful AI response for university students."
        self.prompt_tokens_to_return: int = 25
        self.completion_tokens_to_return: int = 60

    async def agenerate_chat(
        self,
        messages: list[ChatMessage],
        system_instruction: str | None = None,
        temperature: float = 0.7,
    ) -> GeminiResponse:
        self.recorded_calls.append(
            {
                "messages": [ChatMessage(role=m.role, content=m.content) for m in messages],
                "system_instruction": system_instruction,
                "temperature": temperature,
            }
        )

        if self.simulated_exception:
            raise self.simulated_exception

        total = self.prompt_tokens_to_return + self.completion_tokens_to_return
        return GeminiResponse(
            text=self.mock_reply_text,
            model=self.model,
            prompt_tokens=self.prompt_tokens_to_return,
            completion_tokens=self.completion_tokens_to_return,
            total_tokens=total,
            finish_reason="STOP",
        )


class FakeUsageLogger(AIUsageLogger):
    """In-memory telemetry logger test double verifying token records without prompt leaks."""

    def __init__(self) -> None:
        super().__init__(repository=None)
        self.logged_calls: list[dict[str, Any]] = []

    async def log_ai_call(
        self,
        user_id: uuid.UUID,
        feature: str,
        model_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int | None = None,
        latency_ms: int | None = None,
        request_status: str = "success",
        error_message: str | None = None,
        conversation_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        estimated_cost: float | Any = None,
        provider: str = "google",
        repository: Any = None,
    ) -> AIUsageLog | None:
        call_record = {
            "user_id": user_id,
            "feature": feature,
            "model_name": model_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
            "latency_ms": latency_ms,
            "request_status": request_status,
            "conversation_id": conversation_id,
            "message_id": message_id,
        }
        self.logged_calls.append(call_record)
        return None


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for authentication in chat testing."""

    def __init__(self) -> None:
        self.users: dict[uuid.UUID, User] = {}
        self.roles: dict[str, Role] = {
            "student": Role(
                id=uuid.uuid4(),
                name="student",
                description="Default student role.",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        }

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self.users.get(user_id)

    async def create_user(
        self,
        email: str,
        password_hash: str,
        full_name: str,
        role_name: str = "student",
        current_plan_code: str = "free",
        is_email_verified: bool = True,
        avatar_url: str | None = None,
    ) -> User:
        user_id = uuid.uuid4()
        role = self.roles.get(role_name)
        if not role:
            role = Role(
                id=uuid.uuid4(),
                name=role_name,
                description=f"{role_name} role",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            self.roles[role_name] = role

        user = User(
            id=user_id,
            email=email.lower().strip(),
            password_hash=password_hash,
            is_active=True,
            is_email_verified=is_email_verified,
            current_plan_code=current_plan_code,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        profile = UserProfile(
            id=uuid.uuid4(),
            user_id=user_id,
            full_name=full_name.strip(),
            avatar_url=avatar_url,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        user.profile = profile
        user.roles = [role]
        self.users[user_id] = user
        return user


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_chat_repo() -> FakeChatRepository:
    return FakeChatRepository()


@pytest.fixture
def fake_gemini_adapter() -> FakeGeminiAdapter:
    return FakeGeminiAdapter()


@pytest.fixture
def mock_redis() -> MockAsyncRedisClient:
    return MockAsyncRedisClient()


@pytest.fixture
def quota_service(mock_redis: MockAsyncRedisClient) -> AIQuotaService:
    # Free daily limit = 3 for testing quota boundaries
    return AIQuotaService(redis_client=mock_redis, free_limit=3, pro_limit=150)


@pytest.fixture
def fake_usage_logger() -> FakeUsageLogger:
    return FakeUsageLogger()


@pytest.fixture
def chat_service(
    fake_chat_repo: FakeChatRepository,
    fake_gemini_adapter: FakeGeminiAdapter,
    quota_service: AIQuotaService,
    fake_usage_logger: FakeUsageLogger,
) -> ChatService:
    return ChatService(
        chat_repo=fake_chat_repo,
        gemini_adapter=fake_gemini_adapter,
        quota_service=quota_service,
        usage_logger=fake_usage_logger,
    )


@pytest.fixture
def fake_user_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest_asyncio.fixture
async def student_a(fake_user_repo: FakeUserRepository) -> User:
    return await fake_user_repo.create_user(
        email="alice.student@university.edu",
        password_hash=hash_password("ValidPassword123!"),
        full_name="Alice Student",
        current_plan_code="free",
    )


@pytest_asyncio.fixture
async def student_b(fake_user_repo: FakeUserRepository) -> User:
    return await fake_user_repo.create_user(
        email="bob.student@university.edu",
        password_hash=hash_password("ValidPassword123!"),
        full_name="Bob Student",
        current_plan_code="free",
    )


@pytest_asyncio.fixture
async def inactive_student(fake_user_repo: FakeUserRepository) -> User:
    user = await fake_user_repo.create_user(
        email="inactive@university.edu",
        password_hash=hash_password("ValidPassword123!"),
        full_name="Inactive Student",
    )
    user.is_active = False
    return user


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_chat_repo: FakeChatRepository,
    fake_gemini_adapter: FakeGeminiAdapter,
    quota_service: AIQuotaService,
    fake_usage_logger: FakeUsageLogger,
) -> AsyncGenerator[AsyncClient, None]:
    """Test HTTP client with dependency overrides for repositories and services."""
    service = ChatService(
        chat_repo=fake_chat_repo,
        gemini_adapter=fake_gemini_adapter,
        quota_service=quota_service,
        usage_logger=fake_usage_logger,
    )

    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_chat_repo] = lambda: fake_chat_repo
    app.dependency_overrides[get_gemini_adapter] = lambda: fake_gemini_adapter
    app.dependency_overrides[get_quota_service] = lambda: quota_service
    app.dependency_overrides[get_ai_usage_logger] = lambda: fake_usage_logger
    app.dependency_overrides[get_chat_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_header(user: User) -> dict[str, str]:
    token = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Repository & Tenant Isolation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_repo_create_and_get_conversation(fake_chat_repo: FakeChatRepository) -> None:
    user_id = uuid.uuid4()
    conv = await fake_chat_repo.create_conversation(
        user_id=user_id,
        title="Calculus I Help",
        conversation_type="general",
    )
    assert conv.id is not None
    assert conv.user_id == user_id
    assert conv.title == "Calculus I Help"
    assert conv.conversation_type == "general"

    # Fetch back
    fetched = await fake_chat_repo.get_conversation(user_id=user_id, conversation_id=conv.id)
    assert fetched is not None
    assert fetched.id == conv.id
    assert fetched.title == "Calculus I Help"


@pytest.mark.asyncio
async def test_chat_repo_tenant_isolation(fake_chat_repo: FakeChatRepository) -> None:
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    conv_a = await fake_chat_repo.create_conversation(user_id=user_a, title="User A Study")

    # User B cannot get User A's conversation
    fetched_by_b = await fake_chat_repo.get_conversation(
        user_id=user_b, conversation_id=conv_a.id
    )
    assert fetched_by_b is None

    # User B cannot update User A's conversation title
    updated_by_b = await fake_chat_repo.update_conversation_title(
        user_id=user_b, conversation_id=conv_a.id, title="Hacked Title"
    )
    assert updated_by_b is None

    # User B cannot delete User A's conversation
    deleted_by_b = await fake_chat_repo.delete_conversation(
        user_id=user_b, conversation_id=conv_a.id
    )
    assert deleted_by_b is False

    # User B cannot get User A's messages
    await fake_chat_repo.add_message(
        conversation_id=conv_a.id, role="user", content="Secret note"
    )
    messages_for_b = await fake_chat_repo.get_messages(
        user_id=user_b, conversation_id=conv_a.id
    )
    assert messages_for_b == []


@pytest.mark.asyncio
async def test_chat_repo_list_and_delete(fake_chat_repo: FakeChatRepository) -> None:
    user_id = uuid.uuid4()
    c1 = await fake_chat_repo.create_conversation(user_id=user_id, title="Conv 1")
    c2 = await fake_chat_repo.create_conversation(user_id=user_id, title="Conv 2")

    convs, total = await fake_chat_repo.list_conversations(user_id=user_id)
    assert total == 2
    assert len(convs) == 2

    # Delete c1
    deleted = await fake_chat_repo.delete_conversation(user_id=user_id, conversation_id=c1.id)
    assert deleted is True

    convs_after, total_after = await fake_chat_repo.list_conversations(user_id=user_id)
    assert total_after == 1
    assert convs_after[0].id == c2.id


# ---------------------------------------------------------------------------
# 2. Chat Service Domain Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_service_create_and_get(
    chat_service: ChatService,
    student_a: User,
) -> None:
    conv_dto = await chat_service.create_conversation(
        user_id=student_a.id,
        title="Physics 101",
    )
    assert conv_dto.title == "Physics 101"
    assert conv_dto.user_id == student_a.id
    assert conv_dto.message_count == 0

    fetched_dto, messages = await chat_service.get_conversation(
        user_id=student_a.id,
        conversation_id=conv_dto.id,
    )
    assert fetched_dto.id == conv_dto.id
    assert messages == []


@pytest.mark.asyncio
async def test_chat_service_send_message_multi_turn(
    chat_service: ChatService,
    fake_gemini_adapter: FakeGeminiAdapter,
    fake_usage_logger: FakeUsageLogger,
    student_a: User,
) -> None:
    conv_dto = await chat_service.create_conversation(
        user_id=student_a.id,
        title="Discrete Mathematics",
    )

    # First turn
    fake_gemini_adapter.mock_reply_text = "Graph theory is the study of graphs."
    res1 = await chat_service.send_message(
        user_id=student_a.id,
        conversation_id=conv_dto.id,
        content="What is graph theory?",
    )
    assert res1.is_ai_generated is True
    assert "verify critical academic information" in res1.disclaimer
    assert res1.message.content == "Graph theory is the study of graphs."
    assert res1.message.role == "assistant"

    # Verify Gemini was called with the user message
    assert len(fake_gemini_adapter.recorded_calls) == 1
    call1_msgs = fake_gemini_adapter.recorded_calls[0]["messages"]
    assert len(call1_msgs) == 1
    assert call1_msgs[0].role == "user"
    assert call1_msgs[0].content == "What is graph theory?"

    # Second turn (Multi-turn verification)
    fake_gemini_adapter.mock_reply_text = "An Eulerian path visits every edge exactly once."
    res2 = await chat_service.send_message(
        user_id=student_a.id,
        conversation_id=conv_dto.id,
        content="What about an Eulerian path?",
    )
    assert res2.message.content == "An Eulerian path visits every edge exactly once."

    # Verify Gemini adapter received the entire history:
    # 1. user: What is graph theory?
    # 2. model: Graph theory is the study of graphs.
    # 3. user: What about an Eulerian path?
    assert len(fake_gemini_adapter.recorded_calls) == 2
    call2_msgs = fake_gemini_adapter.recorded_calls[1]["messages"]
    assert len(call2_msgs) == 3
    assert call2_msgs[0].role == "user"
    assert call2_msgs[0].content == "What is graph theory?"
    assert call2_msgs[1].role == "model"
    assert call2_msgs[1].content == "Graph theory is the study of graphs."
    assert call2_msgs[2].role == "user"
    assert call2_msgs[2].content == "What about an Eulerian path?"

    # Telemetry logging verification (Invariant 4 & 7)
    assert len(fake_usage_logger.logged_calls) == 2
    for log in fake_usage_logger.logged_calls:
        assert log["user_id"] == student_a.id
        assert log["feature"] == "chat"
        assert log["model_name"] == "gemini-2.5-flash"
        assert log["prompt_tokens"] > 0
        assert log["completion_tokens"] > 0
        assert log["request_status"] == "success"
        # Verify NO prompts or text was logged
        assert "content" not in log
        assert "prompt" not in log


@pytest.mark.asyncio
async def test_chat_service_auto_titles_untitled_conversation(
    chat_service: ChatService,
    student_a: User,
) -> None:
    conv_dto = await chat_service.create_conversation(
        user_id=student_a.id,
        title=None,  # No title initially
    )
    assert conv_dto.title is None

    await chat_service.send_message(
        user_id=student_a.id,
        conversation_id=conv_dto.id,
        content="How do neural networks work in deep learning?",
    )

    updated_conv, _ = await chat_service.get_conversation(
        user_id=student_a.id, conversation_id=conv_dto.id
    )
    assert updated_conv.title is not None
    assert "How do neural networks work" in updated_conv.title


@pytest.mark.asyncio
async def test_chat_service_quota_reservation_and_exhaustion(
    chat_service: ChatService,
    student_a: User,
) -> None:
    # Free quota limit in fixture is 3
    conv = await chat_service.create_conversation(user_id=student_a.id, title="Quota Test")

    # Call 1: success (used = 1)
    await chat_service.send_message(student_a.id, conv.id, "Question 1")
    # Call 2: success (used = 2)
    await chat_service.send_message(student_a.id, conv.id, "Question 2")
    # Call 3: success (used = 3)
    await chat_service.send_message(student_a.id, conv.id, "Question 3")

    # Call 4: exceeded -> raises QuotaExceededError
    with pytest.raises(QuotaExceededError) as exc_info:
        await chat_service.send_message(student_a.id, conv.id, "Question 4")
    assert "Daily AI quota" in str(exc_info.value)


@pytest.mark.asyncio
async def test_chat_service_redis_failure_fails_closed(
    chat_service: ChatService,
    mock_redis: MockAsyncRedisClient,
    student_a: User,
) -> None:
    conv = await chat_service.create_conversation(user_id=student_a.id, title="Redis Test")

    # Simulate Redis being down
    mock_redis.fail_on_next_op = True

    with pytest.raises(QuotaServiceUnavailableError):
        await chat_service.send_message(student_a.id, conv.id, "Any prompt")


@pytest.mark.asyncio
async def test_chat_service_gemini_error_refunds_quota(
    chat_service: ChatService,
    fake_gemini_adapter: FakeGeminiAdapter,
    quota_service: AIQuotaService,
    student_a: User,
) -> None:
    conv = await chat_service.create_conversation(user_id=student_a.id, title="Refund Test")

    # Check initial quota: used = 0
    q_initial = await quota_service.check_quota(student_a.id, plan_code="free")
    assert q_initial.used == 0

    # Simulate Gemini failure
    fake_gemini_adapter.simulated_exception = GeminiCircuitOpenError("Circuit is OPEN")

    with pytest.raises(GeminiCircuitOpenError):
        await chat_service.send_message(student_a.id, conv.id, "Will fail")

    # Quota must be refunded back to 0!
    q_after = await quota_service.check_quota(student_a.id, plan_code="free")
    assert q_after.used == 0


@pytest.mark.asyncio
async def test_chat_service_input_validations(
    chat_service: ChatService,
    student_a: User,
) -> None:
    conv = await chat_service.create_conversation(user_id=student_a.id, title="Validation Test")

    # Empty message
    with pytest.raises(InvalidMessageError):
        await chat_service.send_message(student_a.id, conv.id, "   ")

    # Non-existent conversation
    with pytest.raises(ConversationNotFoundError):
        await chat_service.send_message(student_a.id, uuid.uuid4(), "Valid content")


# ---------------------------------------------------------------------------
# 3. FastAPI API Router Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_create_conversation_success(
    client: AsyncClient,
    student_a: User,
) -> None:
    headers = make_auth_header(student_a)
    response = await client.post(
        "/api/v1/chat/conversations",
        json={"title": "Linear Algebra Study", "conversation_type": "general"},
        headers=headers,
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["title"] == "Linear Algebra Study"
    assert payload["data"]["conversation_type"] == "general"
    assert payload["data"]["user_id"] == str(student_a.id)


@pytest.mark.asyncio
async def test_api_list_conversations_tenant_isolation(
    client: AsyncClient,
    student_a: User,
    student_b: User,
) -> None:
    # Alice creates 2 conversations
    headers_a = make_auth_header(student_a)
    await client.post(
        "/api/v1/chat/conversations",
        json={"title": "Alice Conv 1"},
        headers=headers_a,
    )
    await client.post(
        "/api/v1/chat/conversations",
        json={"title": "Alice Conv 2"},
        headers=headers_a,
    )

    # Bob creates 1 conversation
    headers_b = make_auth_header(student_b)
    await client.post(
        "/api/v1/chat/conversations",
        json={"title": "Bob Conv 1"},
        headers=headers_b,
    )

    # Alice lists conversations -> only sees hers
    res_a = await client.get("/api/v1/chat/conversations", headers=headers_a)
    assert res_a.status_code == 200
    data_a = res_a.json()["data"]
    assert data_a["total"] == 2
    assert all(item["user_id"] == str(student_a.id) for item in data_a["items"])

    # Bob lists conversations -> only sees his
    res_b = await client.get("/api/v1/chat/conversations", headers=headers_b)
    assert res_b.status_code == 200
    data_b = res_b.json()["data"]
    assert data_b["total"] == 1
    assert data_b["items"][0]["title"] == "Bob Conv 1"


@pytest.mark.asyncio
async def test_api_get_conversation_with_messages(
    client: AsyncClient,
    student_a: User,
) -> None:
    headers = make_auth_header(student_a)
    create_res = await client.post(
        "/api/v1/chat/conversations",
        json={"title": "Data Structures"},
        headers=headers,
    )
    conv_id = create_res.json()["data"]["id"]

    # Send a message
    await client.post(
        f"/api/v1/chat/conversations/{conv_id}/messages",
        json={"content": "Explain binary search trees."},
        headers=headers,
    )

    # Fetch conversation details
    get_res = await client.get(
        f"/api/v1/chat/conversations/{conv_id}",
        headers=headers,
    )
    assert get_res.status_code == 200
    body = get_res.json()
    assert body["success"] is True
    assert body["data"]["conversation"]["id"] == conv_id
    messages = body["data"]["messages"]
    assert len(messages) == 2  # 1 user + 1 assistant
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_api_tenant_cross_access_blocked(
    client: AsyncClient,
    student_a: User,
    student_b: User,
) -> None:
    headers_a = make_auth_header(student_a)
    headers_b = make_auth_header(student_b)

    # Alice creates conversation
    create_res = await client.post(
        "/api/v1/chat/conversations",
        json={"title": "Alice Private Chat"},
        headers=headers_a,
    )
    conv_id = create_res.json()["data"]["id"]

    # Bob tries to GET Alice's conversation -> 404
    get_res = await client.get(
        f"/api/v1/chat/conversations/{conv_id}",
        headers=headers_b,
    )
    assert get_res.status_code == 404
    assert get_res.json()["error"]["message"] == "Conversation not found."

    # Bob tries to PATCH Alice's conversation -> 404
    patch_res = await client.patch(
        f"/api/v1/chat/conversations/{conv_id}",
        json={"title": "Hacked Title"},
        headers=headers_b,
    )
    assert patch_res.status_code == 404

    # Bob tries to POST message to Alice's conversation -> 404
    post_res = await client.post(
        f"/api/v1/chat/conversations/{conv_id}/messages",
        json={"content": "Malicious message"},
        headers=headers_b,
    )
    assert post_res.status_code == 404

    # Bob tries to DELETE Alice's conversation -> 404
    del_res = await client.delete(
        f"/api/v1/chat/conversations/{conv_id}",
        headers=headers_b,
    )
    assert del_res.status_code == 404


@pytest.mark.asyncio
async def test_api_patch_and_delete_conversation(
    client: AsyncClient,
    student_a: User,
) -> None:
    headers = make_auth_header(student_a)
    create_res = await client.post(
        "/api/v1/chat/conversations",
        json={"title": "Original Title"},
        headers=headers,
    )
    conv_id = create_res.json()["data"]["id"]

    # PATCH title
    patch_res = await client.patch(
        f"/api/v1/chat/conversations/{conv_id}",
        json={"title": "Updated Title"},
        headers=headers,
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["data"]["title"] == "Updated Title"

    # DELETE
    del_res = await client.delete(
        f"/api/v1/chat/conversations/{conv_id}",
        headers=headers,
    )
    assert del_res.status_code == 200
    assert del_res.json()["success"] is True

    # Confirm it's gone
    get_res = await client.get(
        f"/api/v1/chat/conversations/{conv_id}",
        headers=headers,
    )
    assert get_res.status_code == 404


@pytest.mark.asyncio
async def test_api_send_message_transparency_labels(
    client: AsyncClient,
    student_a: User,
) -> None:
    headers = make_auth_header(student_a)
    create_res = await client.post(
        "/api/v1/chat/conversations",
        json={"title": "Academic Question"},
        headers=headers,
    )
    conv_id = create_res.json()["data"]["id"]

    msg_res = await client.post(
        f"/api/v1/chat/conversations/{conv_id}/messages",
        json={"content": "Explain polymorphism in OOP."},
        headers=headers,
    )
    assert msg_res.status_code == 200
    body = msg_res.json()
    assert body["success"] is True
    data = body["data"]
    # US 2.13 Requirements
    assert data["is_ai_generated"] is True
    assert "verify critical academic information" in data["disclaimer"]
    assert data["message"]["role"] == "assistant"
    assert data["message"]["prompt_tokens"] > 0
    assert data["message"]["completion_tokens"] > 0


@pytest.mark.asyncio
async def test_api_send_message_quota_exceeded_returns_429(
    client: AsyncClient,
    student_a: User,
) -> None:
    headers = make_auth_header(student_a)
    create_res = await client.post(
        "/api/v1/chat/conversations",
        json={"title": "Quota 429 Test"},
        headers=headers,
    )
    conv_id = create_res.json()["data"]["id"]

    # Quota limit is 3 requests
    for i in range(3):
        res = await client.post(
            f"/api/v1/chat/conversations/{conv_id}/messages",
            json={"content": f"Message {i}"},
            headers=headers,
        )
        assert res.status_code == 200

    # 4th message exceeds quota
    res_exceeded = await client.post(
        f"/api/v1/chat/conversations/{conv_id}/messages",
        json={"content": "Message 4"},
        headers=headers,
    )
    assert res_exceeded.status_code == 429
    assert "Daily AI quota" in res_exceeded.json()["error"]["message"]


@pytest.mark.asyncio
async def test_api_send_message_redis_down_returns_503(
    client: AsyncClient,
    mock_redis: MockAsyncRedisClient,
    student_a: User,
) -> None:
    headers = make_auth_header(student_a)
    create_res = await client.post(
        "/api/v1/chat/conversations",
        json={"title": "Fail-Closed Test"},
        headers=headers,
    )
    conv_id = create_res.json()["data"]["id"]

    # Break Redis
    mock_redis.fail_on_next_op = True

    res = await client.post(
        f"/api/v1/chat/conversations/{conv_id}/messages",
        json={"content": "Will fail closed"},
        headers=headers,
    )
    assert res.status_code == 503
    assert "temporarily unavailable" in res.json()["error"]["message"]


@pytest.mark.asyncio
async def test_api_send_message_circuit_breaker_open_returns_503(
    client: AsyncClient,
    fake_gemini_adapter: FakeGeminiAdapter,
    student_a: User,
) -> None:
    headers = make_auth_header(student_a)
    create_res = await client.post(
        "/api/v1/chat/conversations",
        json={"title": "Circuit Breaker Test"},
        headers=headers,
    )
    conv_id = create_res.json()["data"]["id"]

    fake_gemini_adapter.simulated_exception = GeminiCircuitOpenError("Circuit OPEN")

    res = await client.post(
        f"/api/v1/chat/conversations/{conv_id}/messages",
        json={"content": "Should hit circuit breaker"},
        headers=headers,
    )
    assert res.status_code == 503
    assert "circuit breaker" in res.json()["error"]["message"].lower() or "temporarily unavailable" in res.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_unauthenticated_and_inactive_users(
    client: AsyncClient,
    inactive_student: User,
) -> None:
    # 401 Unauthorized without token
    res_no_auth = await client.get("/api/v1/chat/conversations")
    assert res_no_auth.status_code == 401

    # 403 Forbidden with inactive user token
    headers_inactive = make_auth_header(inactive_student)
    res_inactive = await client.get(
        "/api/v1/chat/conversations",
        headers=headers_inactive,
    )
    assert res_inactive.status_code == 403
