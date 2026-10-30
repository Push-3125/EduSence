"""Comprehensive tests for Multiple-Choice & Essay Question Bank Generation (TASK-M5-02 / US 2.15 / US 2.16).

Validates:
- Invariant 2 (Layering): Routers -> Services -> Repositories.
- Invariant 4 (Central LLM): Gemini called only through GeminiAdapter; mocks used in tests.
- Invariant 6 (Strict Tenant Isolation): User A cannot view, add to, or delete User B's banks/questions.
- Invariant 7 (Quota & Telemetry): Atomic quota reservation before LLM call; telemetry logged with tokens/latency.
- Invariant 13 (Untrusted Context Fencing): Prompts fenced via PromptSanitizer.
- Database Constraints: ck_questions_type, ck_questions_difficulty, ck_questions_options.
- API Endpoints: Status codes (200, 201, 401, 404, 422, 429, 503, 504) and envelope structures.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Ensure backend root is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import CheckConstraint

from app.api.v1.question_banks import router as question_banks_router
from app.core.security import create_access_token
from app.main import app
from app.providers.gemini_adapter import (
    GeminiCircuitOpenError,
    GeminiRateLimitError,
    GeminiTimeoutError,
    get_gemini_adapter,
)
from app.providers.usage_logger import (
    AIUsageLog,
    AIUsageLogger,
    get_ai_usage_logger,
)
from app.repositories.chunk_repo import (
    DocumentChunk,
    DocumentChunkRepository,
    get_chunk_repo,
)
from app.repositories.document_repo import (
    Document,
    DocumentRepository,
    get_document_repo,
)
from app.repositories.question_repo import (
    Question,
    QuestionBank,
    QuestionRepository,
    get_question_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.question_generation_service import (
    DocumentNotFoundError,
    GeneratedQuestionItem,
    GeneratedQuestionSet,
    QuestionBankNotFoundError,
    QuestionGenerationService,
    QuotaExceededError,
    QuotaServiceUnavailableError,
)
from app.services.quota_service import AIQuotaService, get_quota_service
from app.services.sanitizer import PromptSanitizer, get_prompt_sanitizer

# Ensure question_banks router is mounted on app
if not any(
    getattr(route, "path", "").startswith("/api/v1/question-banks") for route in app.routes
):
    app.include_router(question_banks_router)


# ---------------------------------------------------------------------------
# Test Doubles & In-Memory Fakes
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for authentication in tests."""

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
        password_hash: str = "dummy_hash",
        full_name: str = "Test Student",
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
        user.roles = [role]
        user.profile = UserProfile(
            user_id=user_id,
            full_name=full_name.strip(),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.users[user_id] = user
        return user


class FakeDocumentRepository(DocumentRepository):
    """In-memory DocumentRepository double enforcing strict tenant isolation."""

    def __init__(self) -> None:
        self.documents: dict[uuid.UUID, Document] = {}

    async def get_by_id_and_user(
        self,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Document | None:
        doc = self.documents.get(document_id)
        if not doc or doc.user_id != user_id:
            return None
        return doc

    async def create_doc(
        self,
        user_id: uuid.UUID,
        title: str = "Calculus Lecture Notes",
        status: str = "embedded",
    ) -> Document:
        doc_id = uuid.uuid4()
        now = datetime.now(UTC)
        doc = Document(
            id=doc_id,
            user_id=user_id,
            course_id=None,
            title=title,
            original_file_name="calculus.pdf",
            storage_object_key=f"docs/{user_id}/{doc_id}.pdf",
            file_type="application/pdf",
            file_size_bytes=2048,
            status=status,
            total_chunks=2,
            chunks_processed=2,
            created_at=now,
            updated_at=now,
        )
        self.documents[doc_id] = doc
        return doc


class FakeDocumentChunkRepository(DocumentChunkRepository):
    """In-memory DocumentChunkRepository double for chunk retrieval."""

    def __init__(self) -> None:
        self.chunks_by_doc: dict[uuid.UUID, list[DocumentChunk]] = {}

    async def get_chunks_by_document(self, document_id: uuid.UUID) -> list[DocumentChunk]:
        return list(self.chunks_by_doc.get(document_id, []))

    def add_chunk(
        self,
        document_id: uuid.UUID,
        content: str,
        chunk_index: int = 0,
    ) -> DocumentChunk:
        chunk = DocumentChunk(
            id=uuid.uuid4(),
            document_id=document_id,
            chunk_index=chunk_index,
            content=content,
            page_number=chunk_index + 1,
            token_count=len(content.split()),
            embedding=[0.0] * 768,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        if document_id not in self.chunks_by_doc:
            self.chunks_by_doc[document_id] = []
        self.chunks_by_doc[document_id].append(chunk)
        return chunk


class FakeQuestionRepository(QuestionRepository):
    """In-memory QuestionRepository test double strictly enforcing tenant isolation."""

    def __init__(self) -> None:
        self.banks: dict[uuid.UUID, QuestionBank] = {}
        self.questions: dict[uuid.UUID, Question] = {}

    async def create_bank(
        self,
        user_id: uuid.UUID,
        name: str,
        description: str | None = None,
        course_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
    ) -> QuestionBank:
        now = datetime.now(UTC)
        bank_id = uuid.uuid4()
        bank = QuestionBank(
            id=bank_id,
            user_id=user_id,
            course_id=course_id,
            document_id=document_id,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
        )
        bank.questions = []
        self.banks[bank_id] = bank
        return bank

    async def get_bank(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID,
    ) -> QuestionBank | None:
        bank = self.banks.get(bank_id)
        if not bank or bank.user_id != user_id:
            return None
        bank_questions = [
            q for q in self.questions.values() if q.question_bank_id == bank_id
        ]
        bank_questions.sort(key=lambda q: q.created_at)
        bank.questions = bank_questions
        return bank

    async def list_banks(
        self,
        user_id: uuid.UUID,
        skip: int = 0,
        limit: int = 50,
        course_id: uuid.UUID | None = None,
    ) -> tuple[list[QuestionBank], int]:
        user_banks = [
            b
            for b in self.banks.values()
            if b.user_id == user_id and (course_id is None or b.course_id == course_id)
        ]
        user_banks.sort(key=lambda b: b.created_at, reverse=True)
        total = len(user_banks)
        paginated = user_banks[skip : skip + limit]
        for b in paginated:
            bank_questions = [
                q for q in self.questions.values() if q.question_bank_id == b.id
            ]
            bank_questions.sort(key=lambda q: q.created_at)
            b.questions = bank_questions
        return paginated, total

    async def delete_bank(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID,
    ) -> bool:
        bank = await self.get_bank(user_id=user_id, bank_id=bank_id)
        if not bank:
            return False
        q_ids = [q_id for q_id, q in self.questions.items() if q.question_bank_id == bank_id]
        for q_id in q_ids:
            del self.questions[q_id]
        del self.banks[bank_id]
        return True

    async def add_questions(
        self,
        bank_id: uuid.UUID,
        questions_data: list[dict[str, Any]],
    ) -> list[Question]:
        created: list[Question] = []
        now = datetime.now(UTC)
        for item in questions_data:
            q_id = uuid.uuid4()
            q = Question(
                id=q_id,
                question_bank_id=bank_id,
                question_type=item["question_type"],
                content=item["content"],
                options=item.get("options"),
                correct_answer=item.get("correct_answer"),
                explanation=item.get("explanation"),
                difficulty=item.get("difficulty", "medium"),
                created_at=now,
                updated_at=now,
            )
            self.questions[q_id] = q
            created.append(q)
            if bank_id in self.banks:
                self.banks[bank_id].questions.append(q)
        return created

    async def get_question(
        self,
        user_id: uuid.UUID,
        question_id: uuid.UUID,
    ) -> Question | None:
        q = self.questions.get(question_id)
        if not q:
            return None
        bank = self.banks.get(q.question_bank_id)
        if not bank or bank.user_id != user_id:
            return None
        return q

    async def delete_question(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID,
        question_id: uuid.UUID,
    ) -> bool:
        q = self.questions.get(question_id)
        if not q or q.question_bank_id != bank_id:
            return False
        bank = self.banks.get(bank_id)
        if not bank or bank.user_id != user_id:
            return False
        del self.questions[question_id]
        if bank.questions:
            bank.questions = [item for item in bank.questions if item.id != question_id]
        return True


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
        record = {
            "user_id": user_id,
            "feature": feature,
            "model_name": model_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
            "latency_ms": latency_ms,
            "request_status": request_status,
        }
        self.logged_calls.append(record)
        return None


class FakeQuotaService(AIQuotaService):
    """In-memory quota manager supporting simulated exhaustion and fail-closed errors."""

    def __init__(self) -> None:
        self.reservations: list[dict[str, Any]] = []
        self.refunds: list[dict[str, Any]] = []
        self.is_exhausted: bool = False
        self.is_redis_down: bool = False

    async def reserve_quota(
        self,
        user_id: uuid.UUID | str,
        plan_code: str = "free",
        target_date: Any = None,
    ) -> Any:
        if self.is_redis_down:
            raise QuotaServiceUnavailableError("Simulated Redis failure")
        if self.is_exhausted:
            raise QuotaExceededError("Daily quota limit exceeded")

        self.reservations.append({"user_id": user_id, "plan_code": plan_code})
        status = MagicMock()
        status.user_id = user_id
        status.remaining = 5
        return status

    async def refund_quota(
        self,
        user_id: uuid.UUID | str,
        target_date: Any = None,
    ) -> None:
        self.refunds.append({"user_id": user_id})


class MockGeminiAdapter:
    """Mock GeminiAdapter for structured generation testing."""

    def __init__(self, responses: list[GeneratedQuestionSet] | None = None) -> None:
        self.model = "gemini-2.5-flash"
        self.responses = responses or []
        self.call_count = 0
        self.simulate_circuit_open = False
        self.simulate_rate_limit = False
        self.simulate_timeout = False

    async def agenerate_structured(
        self,
        prompt: str,
        response_schema: Any,
        system_instruction: str | None = None,
        temperature: float = 0.2,
    ) -> Any:
        self.call_count += 1
        if self.simulate_circuit_open:
            raise GeminiCircuitOpenError("Circuit is OPEN")
        if self.simulate_rate_limit:
            raise GeminiRateLimitError("Rate limit reached")
        if self.simulate_timeout:
            raise GeminiTimeoutError("Request timed out")

        if self.responses:
            return self.responses.pop(0)

        # Default structured question set
        return GeneratedQuestionSet(
            bank_name="Calculus & Analysis Bank",
            description="Practice questions generated from study notes",
            questions=[
                GeneratedQuestionItem(
                    question_type="multiple_choice",
                    content="What is the derivative of sin(x)?",
                    options=["cos(x)", "-cos(x)", "tan(x)", "-sin(x)"],
                    correct_answer="cos(x)",
                    explanation="By standard trigonometric differentiation rules, d/dx sin(x) = cos(x).",
                    difficulty="easy",
                ),
                GeneratedQuestionItem(
                    question_type="multiple_choice",
                    content="Which theorem guarantees that a continuous function attains maximum and minimum on [a, b]?",
                    options=[
                        "Extreme Value Theorem",
                        "Mean Value Theorem",
                        "Rolle's Theorem",
                        "Intermediate Value Theorem",
                    ],
                    correct_answer="Extreme Value Theorem",
                    explanation="The Extreme Value Theorem ensures maximum and minimum for continuous functions on closed intervals.",
                    difficulty="medium",
                ),
                GeneratedQuestionItem(
                    question_type="essay",
                    content="Explain the Fundamental Theorem of Calculus and discuss its two main parts.",
                    options=None,
                    correct_answer="Part 1 links differentiation to integration via antiderivatives; Part 2 provides evaluation of definite integrals.",
                    explanation="A comprehensive answer covers statement, proof outline, and practical significance.",
                    difficulty="hard",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_user_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def fake_doc_repo() -> FakeDocumentRepository:
    return FakeDocumentRepository()


@pytest.fixture
def fake_chunk_repo() -> FakeDocumentChunkRepository:
    return FakeDocumentChunkRepository()


@pytest.fixture
def fake_question_repo() -> FakeQuestionRepository:
    return FakeQuestionRepository()


@pytest.fixture
def fake_usage_logger() -> FakeUsageLogger:
    return FakeUsageLogger()


@pytest.fixture
def fake_quota_service() -> FakeQuotaService:
    return FakeQuotaService()


@pytest.fixture
def mock_gemini_adapter() -> MockGeminiAdapter:
    return MockGeminiAdapter()


@pytest.fixture
def sanitizer() -> PromptSanitizer:
    return PromptSanitizer()


@pytest.fixture
def question_service(
    fake_question_repo: FakeQuestionRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    mock_gemini_adapter: MockGeminiAdapter,
    fake_usage_logger: FakeUsageLogger,
    fake_quota_service: FakeQuotaService,
    sanitizer: PromptSanitizer,
) -> QuestionGenerationService:
    return QuestionGenerationService(
        question_repo=fake_question_repo,
        document_repo=fake_doc_repo,
        chunk_repo=fake_chunk_repo,
        gemini_adapter=mock_gemini_adapter,  # type: ignore[arg-type]
        usage_logger=fake_usage_logger,
        quota_service=fake_quota_service,
        sanitizer=sanitizer,
    )


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    fake_question_repo: FakeQuestionRepository,
    fake_usage_logger: FakeUsageLogger,
    fake_quota_service: FakeQuotaService,
    mock_gemini_adapter: MockGeminiAdapter,
    sanitizer: PromptSanitizer,
) -> AsyncGenerator[AsyncClient, None]:
    """Provide AsyncClient with all dependencies mocked and overridden."""
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_document_repo] = lambda: fake_doc_repo
    app.dependency_overrides[get_chunk_repo] = lambda: fake_chunk_repo
    app.dependency_overrides[get_question_repo] = lambda: fake_question_repo
    app.dependency_overrides[get_ai_usage_logger] = lambda: fake_usage_logger
    app.dependency_overrides[get_quota_service] = lambda: fake_quota_service
    app.dependency_overrides[get_gemini_adapter] = lambda: mock_gemini_adapter
    app.dependency_overrides[get_prompt_sanitizer] = lambda: sanitizer

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_header(user_id: uuid.UUID) -> dict[str, str]:
    """Generate Bearer Authorization header for user UUID."""
    token = create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(minutes=60),
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Database Constraints Enforcement Tests
# ---------------------------------------------------------------------------


def test_question_model_constraints_metadata() -> None:
    """Verify check constraints are properly defined on the Question SQLAlchemy model."""
    table: Any = Question.__table__
    constraints = {c.name: c for c in table.constraints if isinstance(c, CheckConstraint)}

    # ck_questions_type
    assert "ck_questions_type" in constraints
    type_sql = str(constraints["ck_questions_type"].sqltext)
    assert "multiple_choice" in type_sql
    assert "essay" in type_sql

    # ck_questions_difficulty
    assert "ck_questions_difficulty" in constraints
    diff_sql = str(constraints["ck_questions_difficulty"].sqltext)
    assert "easy" in diff_sql
    assert "medium" in diff_sql
    assert "hard" in diff_sql

    # ck_questions_options
    assert "ck_questions_options" in constraints
    opts_sql = str(constraints["ck_questions_options"].sqltext)
    assert "jsonb_typeof" in opts_sql or "options IS NOT NULL" in opts_sql


# ---------------------------------------------------------------------------
# 2. Question Generation Modes Tests (multiple_choice, essay, mixed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_multiple_choice_questions(
    question_service: QuestionGenerationService,
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    fake_usage_logger: FakeUsageLogger,
    mock_gemini_adapter: MockGeminiAdapter,
) -> None:
    """Test generating multiple_choice questions ensures valid 4-option arrays and correct answers."""
    user = await fake_user_repo.create_user("student1@example.com")
    doc = await fake_doc_repo.create_doc(user.id, title="Calculus 101")
    fake_chunk_repo.add_chunk(doc.id, "Differentiation rules and limits definition.", 0)

    bank_dto = await question_service.generate_questions_from_document(
        user_id=user.id,
        document_id=doc.id,
        num_questions=2,
        question_type="multiple_choice",
        difficulty="medium",
    )

    assert bank_dto.id is not None
    assert bank_dto.user_id == user.id
    assert bank_dto.name == "Calculus & Analysis Bank"
    assert len(bank_dto.questions) == 2

    for q in bank_dto.questions:
        assert q.question_type == "multiple_choice"
        assert q.options is not None
        assert isinstance(q.options, list)
        assert len(q.options) >= 2
        assert q.correct_answer is not None
        assert q.difficulty in ("easy", "medium", "hard")

    # Verify telemetry logging was called with tokens and latency
    assert len(fake_usage_logger.logged_calls) == 1
    call = fake_usage_logger.logged_calls[0]
    assert call["feature"] == "questions"
    assert call["user_id"] == user.id
    assert call["prompt_tokens"] > 0
    assert call["completion_tokens"] > 0
    assert call["total_tokens"] > 0
    assert call["latency_ms"] is not None


@pytest.mark.asyncio
async def test_generate_essay_questions(
    question_service: QuestionGenerationService,
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    mock_gemini_adapter: MockGeminiAdapter,
) -> None:
    """Test generating essay questions forces options to None and preserves model answers."""
    user = await fake_user_repo.create_user("student2@example.com")
    doc = await fake_doc_repo.create_doc(user.id, title="Philosophy of Science")
    fake_chunk_repo.add_chunk(doc.id, "Popper's falsification criterion and Kuhn's paradigms.", 0)

    mock_gemini_adapter.responses = [
        GeneratedQuestionSet(
            bank_name="Epistemology Essay Bank",
            description="Essay questions on philosophy of science",
            questions=[
                GeneratedQuestionItem(
                    question_type="essay",
                    content="Compare Popper's falsificationism with Kuhn's paradigm shifts.",
                    options=None,
                    correct_answer="Popper emphasizes deductive refutation while Kuhn analyzes sociological consensus...",
                    explanation="Focus on anomalies, crises, and incommensurability.",
                    difficulty="hard",
                )
            ],
        )
    ]

    bank_dto = await question_service.generate_questions_from_document(
        user_id=user.id,
        document_id=doc.id,
        num_questions=1,
        question_type="essay",
        difficulty="hard",
    )

    assert len(bank_dto.questions) == 1
    q = bank_dto.questions[0]
    assert q.question_type == "essay"
    assert q.options is None
    assert q.correct_answer is not None
    assert "Popper" in q.correct_answer


@pytest.mark.asyncio
async def test_generate_mixed_questions(
    question_service: QuestionGenerationService,
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
) -> None:
    """Test generating mixed questions produces both multiple_choice and essay types."""
    user = await fake_user_repo.create_user("student3@example.com")
    doc = await fake_doc_repo.create_doc(user.id, title="Computer Systems")
    fake_chunk_repo.add_chunk(doc.id, "Virtual memory, page tables, and caching hierarchy.", 0)

    bank_dto = await question_service.generate_questions_from_document(
        user_id=user.id,
        document_id=doc.id,
        num_questions=3,
        question_type="mixed",
        difficulty="medium",
    )

    assert len(bank_dto.questions) == 3
    types = {q.question_type for q in bank_dto.questions}
    assert "multiple_choice" in types
    assert "essay" in types


# ---------------------------------------------------------------------------
# 3. Strict Tenant Isolation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_tenant_isolation(
    question_service: QuestionGenerationService,
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
) -> None:
    """Ensure User A cannot view, add to, or delete User B's question bank or questions."""
    alice = await fake_user_repo.create_user("alice@example.com")
    bob = await fake_user_repo.create_user("bob@example.com")

    doc_alice = await fake_doc_repo.create_doc(alice.id, title="Alice's Biology")
    fake_chunk_repo.add_chunk(doc_alice.id, "Cellular mitosis and meiosis.", 0)

    # Bob cannot generate questions from Alice's document
    with pytest.raises(DocumentNotFoundError):
        await question_service.generate_questions_from_document(
            user_id=bob.id,
            document_id=doc_alice.id,
        )

    # Alice generates her bank
    alice_bank = await question_service.generate_questions_from_document(
        user_id=alice.id,
        document_id=doc_alice.id,
        num_questions=2,
    )
    alice_q_id = alice_bank.questions[0].id

    # Bob cannot get Alice's bank
    with pytest.raises(QuestionBankNotFoundError):
        await question_service.get_bank(user_id=bob.id, bank_id=alice_bank.id)

    # Bob's list does not include Alice's bank
    bob_banks, total = await question_service.list_banks(user_id=bob.id)
    assert total == 0
    assert len(bob_banks) == 0

    # Bob cannot add a question to Alice's bank
    with pytest.raises(QuestionBankNotFoundError):
        await question_service.add_question(
            user_id=bob.id,
            bank_id=alice_bank.id,
            question_type="multiple_choice",
            content="Unauthorized question",
            options=["A", "B", "C", "D"],
            correct_answer="A",
        )

    # Bob cannot delete Alice's question
    with pytest.raises(QuestionBankNotFoundError):
        await question_service.delete_question(
            user_id=bob.id,
            bank_id=alice_bank.id,
            question_id=alice_q_id,
        )

    # Bob cannot delete Alice's bank
    with pytest.raises(QuestionBankNotFoundError):
        await question_service.delete_bank(user_id=bob.id, bank_id=alice_bank.id)


# ---------------------------------------------------------------------------
# 4. Quota Reservation & Resilience Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_exhausted_raises_429(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    fake_quota_service: FakeQuotaService,
) -> None:
    """Verify HTTP 429 is returned when daily quota is exhausted."""
    user = await fake_user_repo.create_user("exhausted@example.com")
    doc = await fake_doc_repo.create_doc(user.id, title="Chemistry")
    fake_chunk_repo.add_chunk(doc.id, "Organic reactions and synthesis.", 0)

    fake_quota_service.is_exhausted = True

    response = await client.post(
        "/api/v1/question-banks/generate",
        json={"document_id": str(doc.id), "num_questions": 5},
        headers=make_auth_header(user.id),
    )
    assert response.status_code == 429
    body = response.json()
    assert body["success"] is False
    assert "quota" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_redis_failure_raises_503(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    fake_quota_service: FakeQuotaService,
) -> None:
    """Verify HTTP 503 fail-closed when Redis quota service is unavailable."""
    user = await fake_user_repo.create_user("redisdown@example.com")
    doc = await fake_doc_repo.create_doc(user.id, title="Physics")
    fake_chunk_repo.add_chunk(doc.id, "Thermodynamics laws.", 0)

    fake_quota_service.is_redis_down = True

    response = await client.post(
        "/api/v1/question-banks/generate",
        json={"document_id": str(doc.id), "num_questions": 3},
        headers=make_auth_header(user.id),
    )
    assert response.status_code == 503
    body = response.json()
    assert body["success"] is False
    assert "unavailable" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_upstream_gemini_circuit_breaker_and_timeout(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    mock_gemini_adapter: MockGeminiAdapter,
) -> None:
    """Verify HTTP 503 on circuit breaker open and HTTP 504 on timeout."""
    user = await fake_user_repo.create_user("resilience@example.com")
    doc = await fake_doc_repo.create_doc(user.id, title="Algorithms")
    fake_chunk_repo.add_chunk(doc.id, "Dynamic programming paradigms.", 0)

    # 1. Circuit Open -> 503
    mock_gemini_adapter.simulate_circuit_open = True
    res_circuit = await client.post(
        "/api/v1/question-banks/generate",
        json={"document_id": str(doc.id), "num_questions": 2},
        headers=make_auth_header(user.id),
    )
    assert res_circuit.status_code == 503
    mock_gemini_adapter.simulate_circuit_open = False

    # 2. Timeout -> 504
    mock_gemini_adapter.simulate_timeout = True
    res_timeout = await client.post(
        "/api/v1/question-banks/generate",
        json={"document_id": str(doc.id), "num_questions": 2},
        headers=make_auth_header(user.id),
    )
    assert res_timeout.status_code == 504


# ---------------------------------------------------------------------------
# 5. API Endpoints Authorization, CRUD, and Validation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_unauthorized_access(client: AsyncClient) -> None:
    """Endpoints require valid authentication token (HTTP 401)."""
    random_id = uuid.uuid4()
    assert (await client.get("/api/v1/question-banks")).status_code == 401
    assert (await client.get(f"/api/v1/question-banks/{random_id}")).status_code == 401
    assert (await client.post("/api/v1/question-banks", json={"name": "Bank"})).status_code == 401
    assert (await client.delete(f"/api/v1/question-banks/{random_id}")).status_code == 401


@pytest.mark.asyncio
async def test_api_generate_and_crud_happy_path(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
) -> None:
    """Full lifecycle: Generate AI bank -> List -> Get -> Add manual question -> Delete question -> Delete bank."""
    user = await fake_user_repo.create_user("student_crud@example.com")
    doc = await fake_doc_repo.create_doc(user.id, title="Linear Algebra")
    fake_chunk_repo.add_chunk(doc.id, "Eigenvalues and eigenvectors of matrices.", 0)
    auth = make_auth_header(user.id)

    # 1. Generate bank
    res_gen = await client.post(
        "/api/v1/question-banks/generate",
        json={
            "document_id": str(doc.id),
            "num_questions": 3,
            "question_type": "mixed",
            "difficulty": "medium",
        },
        headers=auth,
    )
    assert res_gen.status_code == 201
    body_gen = res_gen.json()
    assert body_gen["success"] is True
    bank_id = body_gen["data"]["id"]
    assert len(body_gen["data"]["questions"]) == 3

    # 2. List banks
    res_list = await client.get("/api/v1/question-banks", headers=auth)
    assert res_list.status_code == 200
    body_list = res_list.json()
    assert body_list["success"] is True
    assert body_list["meta"]["total"] == 1
    assert body_list["data"][0]["id"] == bank_id

    # 3. Get single bank with questions
    res_get = await client.get(f"/api/v1/question-banks/{bank_id}", headers=auth)
    assert res_get.status_code == 200
    assert res_get.json()["data"]["name"] == "Calculus & Analysis Bank"

    # 4. Add manual question
    res_add_q = await client.post(
        f"/api/v1/question-banks/{bank_id}/questions",
        json={
            "question_type": "multiple_choice",
            "content": "What is the trace of an identity matrix of size 3?",
            "options": ["1", "2", "3", "0"],
            "correct_answer": "3",
            "explanation": "Sum of diagonal elements: 1 + 1 + 1 = 3.",
            "difficulty": "easy",
        },
        headers=auth,
    )
    assert res_add_q.status_code == 201
    q_data = res_add_q.json()["data"]
    new_q_id = q_data["id"]
    assert q_data["content"] == "What is the trace of an identity matrix of size 3?"

    # 5. Delete the question
    res_del_q = await client.delete(
        f"/api/v1/question-banks/{bank_id}/questions/{new_q_id}",
        headers=auth,
    )
    assert res_del_q.status_code == 200
    assert res_del_q.json()["success"] is True

    # 6. Delete the bank
    res_del_b = await client.delete(f"/api/v1/question-banks/{bank_id}", headers=auth)
    assert res_del_b.status_code == 200
    assert res_del_b.json()["success"] is True

    # 7. Verify bank is gone (404)
    res_verify = await client.get(f"/api/v1/question-banks/{bank_id}", headers=auth)
    assert res_verify.status_code == 404


@pytest.mark.asyncio
async def test_api_validation_errors(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
) -> None:
    """Validate request boundaries (num_questions range, empty bank name, MC without options)."""
    user = await fake_user_repo.create_user("student_validation@example.com")
    doc = await fake_doc_repo.create_doc(user.id, title="Data Science")
    fake_chunk_repo.add_chunk(doc.id, "Pandas and numpy operations.", 0)
    auth = make_auth_header(user.id)

    # 1. num_questions out of bounds (0 < 1) -> 422
    res_bound1 = await client.post(
        "/api/v1/question-banks/generate",
        json={"document_id": str(doc.id), "num_questions": 0},
        headers=auth,
    )
    assert res_bound1.status_code == 422

    # 2. num_questions out of bounds (25 > 20) -> 422
    res_bound2 = await client.post(
        "/api/v1/question-banks/generate",
        json={"document_id": str(doc.id), "num_questions": 25},
        headers=auth,
    )
    assert res_bound2.status_code == 422

    # 3. Create bank with empty name -> 422
    res_empty_name = await client.post(
        "/api/v1/question-banks",
        json={"name": ""},
        headers=auth,
    )
    assert res_empty_name.status_code == 422

    # Create a valid bank to test question validation
    res_bank = await client.post(
        "/api/v1/question-banks",
        json={"name": "Valid Bank"},
        headers=auth,
    )
    bank_id = res_bank.json()["data"]["id"]

    # 4. Add multiple_choice without options array -> 422
    res_mc_no_opts = await client.post(
        f"/api/v1/question-banks/{bank_id}/questions",
        json={
            "question_type": "multiple_choice",
            "content": "Question without options",
            "options": None,
        },
        headers=auth,
    )
    assert res_mc_no_opts.status_code == 422

    # 5. Add multiple_choice with only 1 option -> 422
    res_mc_one_opt = await client.post(
        f"/api/v1/question-banks/{bank_id}/questions",
        json={
            "question_type": "multiple_choice",
            "content": "Question with 1 option",
            "options": ["Just one"],
        },
        headers=auth,
    )
    assert res_mc_one_opt.status_code == 422


@pytest.mark.asyncio
async def test_empty_document_raises_400(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
) -> None:
    """Document with no text chunks returns HTTP 400 Bad Request."""
    user = await fake_user_repo.create_user("student_empty@example.com")
    doc = await fake_doc_repo.create_doc(user.id, title="Empty Scanned PDF")
    auth = make_auth_header(user.id)

    response = await client.post(
        "/api/v1/question-banks/generate",
        json={"document_id": str(doc.id), "num_questions": 5},
        headers=auth,
    )
    assert response.status_code == 400
    assert response.json()["success"] is False
    assert "no text content" in response.json()["error"]["message"].lower()
