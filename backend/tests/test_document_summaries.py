"""Comprehensive tests for Document Summarization Service (TASK-M5-01 / US 2.14 / US 2.15).

Validates:
- Invariant 2 (Layering): Routers -> Services -> Repositories.
- Invariant 4: Central LLM Adapter (Gemini called only through GeminiAdapter; mocks used in tests).
- Invariant 6: Strict Tenant Isolation (User A cannot view, summarize, delete User B's documents/summaries/jobs).
- Invariant 7: Atomic Quota Reservation before LLM calls (exceeded -> 429, Redis down -> 503 fail-closed).
- Invariant 8 & 9: Long AI tasks return job_id immediately; client polls; idempotent Celery task execution.
- Invariant 13: Untrusted context isolated within boundary fences via PromptSanitizer.
- Standard Envelope: { success: true, data: ..., message: ... } for all successful responses.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure backend root is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.jobs import router as jobs_router
from app.api.v1.summaries import router as summaries_router
from app.core.security import create_access_token
from app.main import app
from app.providers.gemini_adapter import (
    GeminiCircuitOpenError,
    GeminiRateLimitError,
    GeminiResponse,
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
from app.repositories.job_repo import (
    BackgroundJob,
    JobRepository,
    get_job_repo,
)
from app.repositories.summary_repo import (
    DocumentSummary,
    SummaryRepository,
    get_summary_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.quota_service import (
    AIQuotaService,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    get_quota_service,
)
from app.worker.tasks.summary_tasks import generate_summary_task

# Ensure routers are mounted
if not any(getattr(route, "path", "").startswith("/api/v1/documents/{document_id}/summaries") for route in app.routes):
    app.include_router(summaries_router)
if not any(getattr(route, "path", "").startswith("/api/v1/jobs") for route in app.routes):
    app.include_router(jobs_router)


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
        title: str = "Data Structures & Algorithms",
        status: str = "embedded",
    ) -> Document:
        doc_id = uuid.uuid4()
        doc = Document(
            id=doc_id,
            user_id=user_id,
            course_id=None,
            title=title,
            original_file_name="lecture1.pdf",
            storage_object_key=f"docs/{user_id}/{doc_id}.pdf",
            file_type="application/pdf",
            file_size_bytes=1024,
            status=status,
            total_chunks=3,
            chunks_processed=3,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
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


class FakeSummaryRepository(SummaryRepository):
    """In-memory SummaryRepository double enforcing tenant isolation."""

    def __init__(self) -> None:
        self.summaries: dict[uuid.UUID, DocumentSummary] = {}

    async def create_summary(
        self,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        summary_type: str,
        content: str,
        model_name: str | None = None,
    ) -> DocumentSummary:
        now = datetime.now(UTC)
        summary_id = uuid.uuid4()
        summary = DocumentSummary(
            id=summary_id,
            document_id=document_id,
            user_id=user_id,
            summary_type=summary_type,
            content=content,
            model_name=model_name or "gemini-2.5-flash",
            created_at=now,
            updated_at=now,
        )
        self.summaries[summary_id] = summary
        return summary

    async def get_summary(
        self,
        user_id: uuid.UUID,
        summary_id: uuid.UUID,
    ) -> DocumentSummary | None:
        summary = self.summaries.get(summary_id)
        if not summary or summary.user_id != user_id:
            return None
        return summary

    async def list_summaries_by_document(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> list[DocumentSummary]:
        res = [
            s
            for s in self.summaries.values()
            if s.document_id == document_id and s.user_id == user_id
        ]
        res.sort(key=lambda s: s.created_at, reverse=True)
        return res

    async def delete_summary(
        self,
        user_id: uuid.UUID,
        summary_id: uuid.UUID,
    ) -> bool:
        summary = await self.get_summary(user_id=user_id, summary_id=summary_id)
        if not summary:
            return False
        del self.summaries[summary_id]
        return True


class FakeJobRepository(JobRepository):
    """In-memory JobRepository double for tracking background AI jobs."""

    def __init__(self) -> None:
        self.jobs: dict[uuid.UUID, BackgroundJob] = {}

    async def create_job(
        self,
        user_id: uuid.UUID,
        job_type: str,
        idempotency_key: str | None = None,
    ) -> BackgroundJob:
        job_id = uuid.uuid4()
        now = datetime.now(UTC)
        job = BackgroundJob(
            id=job_id,
            user_id=user_id,
            job_type=job_type,
            status="queued",
            progress=0,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
            queued_at=now,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job_id] = job
        return job

    async def get_job(
        self,
        user_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> BackgroundJob | None:
        job = self.jobs.get(job_id)
        if not job or job.user_id != user_id:
            return None
        return job

    async def update_job_status(
        self,
        job_id: uuid.UUID,
        status: str,
        progress_percent: int = 0,
        result_payload: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> BackgroundJob | None:
        job = self.jobs.get(job_id)
        if not job:
            return None
        clean_status = status.lower().strip()
        db_status = "processing" if clean_status == "running" else (
            "succeeded" if clean_status == "completed" else clean_status
        )
        job.status = db_status
        job.progress = progress_percent
        if result_payload is not None:
            job.result = result_payload
        if error_code is not None:
            job.error_code = error_code
        if error_message is not None:
            job.error_message = error_message
        now = datetime.now(UTC)
        if db_status == "processing" and not job.started_at:
            job.started_at = now
        if db_status in ("succeeded", "failed", "cancelled"):
            job.finished_at = now
        return job


class FakeGeminiAdapter:
    """Mock GeminiAdapter avoiding external API calls while recording requests."""

    def __init__(self) -> None:
        self.model = "gemini-2.5-flash"
        self.recorded_calls: list[dict[str, Any]] = []
        self.simulated_exception: Exception | None = None
        self.mock_text_response: str = (
            "### Summary\n\nThis academic chapter examines binary search trees and heap structures."
        )

    async def agenerate_text(
        self,
        prompt: str,
        system_instruction: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
    ) -> GeminiResponse:
        self.recorded_calls.append(
            {
                "prompt": prompt,
                "system_instruction": system_instruction,
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
            }
        )
        if self.simulated_exception:
            raise self.simulated_exception

        return GeminiResponse(
            text=self.mock_text_response,
            model=self.model,
            prompt_tokens=150,
            completion_tokens=85,
            total_tokens=235,
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
    """In-memory quota manager supporting simulated quota exhaustion and fail-closed errors."""

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
def fake_summary_repo() -> FakeSummaryRepository:
    return FakeSummaryRepository()


@pytest.fixture
def fake_job_repo() -> FakeJobRepository:
    return FakeJobRepository()


@pytest.fixture
def fake_gemini() -> FakeGeminiAdapter:
    return FakeGeminiAdapter()


@pytest.fixture
def fake_logger() -> FakeUsageLogger:
    return FakeUsageLogger()


@pytest.fixture
def fake_quota() -> FakeQuotaService:
    return FakeQuotaService()


@pytest_asyncio.fixture
async def setup_environment(
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    fake_summary_repo: FakeSummaryRepository,
    fake_job_repo: FakeJobRepository,
    fake_gemini: FakeGeminiAdapter,
    fake_logger: FakeUsageLogger,
    fake_quota: FakeQuotaService,
) -> AsyncGenerator[dict[str, Any], None]:
    """Configure dependency overrides for FastAPI application."""
    user_a = await fake_user_repo.create_user(
        email="student.a@university.edu",
        full_name="Alice Student",
        current_plan_code="free",
    )
    user_b = await fake_user_repo.create_user(
        email="student.b@university.edu",
        full_name="Bob Student",
        current_plan_code="pro",
    )

    doc_a = await fake_doc_repo.create_doc(
        user_id=user_a.id,
        title="Operating Systems Memory Hierarchy",
    )
    fake_chunk_repo.add_chunk(
        doc_a.id,
        "Virtual memory maps virtual addresses used by programs to physical addresses in RAM.",
        chunk_index=0,
    )
    fake_chunk_repo.add_chunk(
        doc_a.id,
        "Page replacement algorithms such as LRU and Clock manage eviction when frames are full.",
        chunk_index=1,
    )

    doc_b = await fake_doc_repo.create_doc(
        user_id=user_b.id,
        title="Microeconomics Principles",
    )
    fake_chunk_repo.add_chunk(
        doc_b.id,
        "Supply and demand determine equilibrium prices in competitive markets.",
        chunk_index=0,
    )

    token_a = create_access_token(subject=str(user_a.id))
    token_b = create_access_token(subject=str(user_b.id))

    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_document_repo] = lambda: fake_doc_repo
    app.dependency_overrides[get_chunk_repo] = lambda: fake_chunk_repo
    app.dependency_overrides[get_summary_repo] = lambda: fake_summary_repo
    app.dependency_overrides[get_job_repo] = lambda: fake_job_repo
    app.dependency_overrides[get_gemini_adapter] = lambda: fake_gemini
    app.dependency_overrides[get_ai_usage_logger] = lambda: fake_logger
    app.dependency_overrides[get_quota_service] = lambda: fake_quota

    yield {
        "user_a": user_a,
        "user_b": user_b,
        "doc_a": doc_a,
        "doc_b": doc_b,
        "token_a": token_a,
        "token_b": token_b,
        "fake_doc_repo": fake_doc_repo,
        "fake_summary_repo": fake_summary_repo,
        "fake_job_repo": fake_job_repo,
        "fake_gemini": fake_gemini,
        "fake_logger": fake_logger,
        "fake_quota": fake_quota,
        "fake_chunk_repo": fake_chunk_repo,
    }

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_types_validation(
    client: AsyncClient,
    setup_environment: dict[str, Any],
) -> None:
    """Verify that only ('brief', 'general', 'detailed') are accepted."""
    env = setup_environment
    headers = {"Authorization": f"Bearer {env['token_a']}"}
    doc_id = env["doc_a"].id

    # 1. Valid types: brief, general, detailed
    for valid_type in ("brief", "general", "detailed"):
        res = await client.post(
            f"/api/v1/documents/{doc_id}/summaries?summary_type={valid_type}",
            headers=headers,
        )
        assert res.status_code == 201, f"Failed for valid type: {valid_type}"
        data = res.json()
        assert data["success"] is True
        assert data["data"]["summary_type"] == valid_type

    # 2. Invalid types reject with 400 Bad Request
    for invalid_type in ("ultra_brief", "executive", "random_string"):
        res = await client.post(
            f"/api/v1/documents/{doc_id}/summaries?summary_type={invalid_type}",
            headers=headers,
        )
        assert res.status_code == 400
        assert "Invalid summary type" in res.json()["error"]["message"]


@pytest.mark.asyncio
async def test_sync_summary_generation_happy_path(
    client: AsyncClient,
    setup_environment: dict[str, Any],
) -> None:
    """Verify synchronous summary generation, prompt isolation, telemetry, and quota."""
    env = setup_environment
    headers = {"Authorization": f"Bearer {env['token_a']}"}
    doc_id = env["doc_a"].id
    fake_gemini: FakeGeminiAdapter = env["fake_gemini"]
    fake_logger: FakeUsageLogger = env["fake_logger"]
    fake_quota: FakeQuotaService = env["fake_quota"]

    res = await client.post(
        f"/api/v1/documents/{doc_id}/summaries?summary_type=detailed",
        headers=headers,
    )
    assert res.status_code == 201
    body = res.json()
    assert body["success"] is True
    assert body["message"] == "Summary generated successfully."

    summary_data = body["data"]
    assert summary_data["summary_type"] == "detailed"
    assert "Virtual memory" in fake_gemini.recorded_calls[0]["prompt"]
    assert "<untrusted_student_context>" in fake_gemini.recorded_calls[0]["prompt"]

    # Telemetry logging verified
    assert len(fake_logger.logged_calls) == 1
    call = fake_logger.logged_calls[0]
    assert call["feature"] == "summary"
    assert call["user_id"] == env["user_a"].id
    assert call["total_tokens"] == 235
    assert call["request_status"] == "success"

    # Quota reservation verified
    assert len(fake_quota.reservations) == 1
    assert fake_quota.reservations[0]["user_id"] == env["user_a"].id
    assert fake_quota.reservations[0]["plan_code"] == "free"


@pytest.mark.asyncio
async def test_async_summary_job_creation_and_polling(
    client: AsyncClient,
    setup_environment: dict[str, Any],
) -> None:
    """Verify async summary returns 202 Accepted and job status endpoint allows polling."""
    env = setup_environment
    headers = {"Authorization": f"Bearer {env['token_a']}"}
    doc_id = env["doc_a"].id
    fake_job_repo: FakeJobRepository = env["fake_job_repo"]

    # 1. Trigger async summary job
    with patch("app.worker.tasks.summary_tasks.generate_summary_task.delay") as mock_delay:
        res = await client.post(
            f"/api/v1/documents/{doc_id}/summaries?async_mode=true&summary_type=general",
            headers=headers,
        )
        assert res.status_code == 202
        body = res.json()
        assert body["success"] is True
        assert body["message"] == "Summary generation job queued."

        job_data = body["data"]
        job_id = uuid.UUID(job_data["id"])
        assert job_data["status"] == "queued"
        assert job_data["progress_percent"] == 0

        # Verify Celery delay dispatch
        mock_delay.assert_called_once_with(
            str(job_id),
            str(doc_id),
            str(env["user_a"].id),
            "general",
            "free",
        )

    # 2. Poll job status
    poll_res = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert poll_res.status_code == 200
    poll_body = poll_res.json()
    assert poll_body["success"] is True
    assert poll_body["data"]["id"] == str(job_id)
    assert poll_body["data"]["status"] == "queued"

    # 3. Simulate job progress in worker
    await fake_job_repo.update_job_status(
        job_id=job_id,
        status="running",
        progress_percent=50,
    )
    poll_running = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert poll_running.json()["data"]["status"] == "running"
    assert poll_running.json()["data"]["progress_percent"] == 50

    # 4. Simulate job completion
    summary_id = str(uuid.uuid4())
    await fake_job_repo.update_job_status(
        job_id=job_id,
        status="completed",
        progress_percent=100,
        result_payload={"summary_id": summary_id},
    )
    poll_done = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert poll_done.json()["data"]["status"] == "completed"
    assert poll_done.json()["data"]["progress_percent"] == 100
    assert poll_done.json()["data"]["result_payload"]["summary_id"] == summary_id


@pytest.mark.asyncio
async def test_strict_tenant_isolation(
    client: AsyncClient,
    setup_environment: dict[str, Any],
) -> None:
    """Verify that User B cannot access, summarize, view, or delete User A's data."""
    env = setup_environment
    headers_b = {"Authorization": f"Bearer {env['token_b']}"}
    headers_a = {"Authorization": f"Bearer {env['token_a']}"}
    doc_a_id = env["doc_a"].id

    # 1. User B attempts to generate summary for User A's document -> 404
    res_unauth_gen = await client.post(
        f"/api/v1/documents/{doc_a_id}/summaries",
        headers=headers_b,
    )
    assert res_unauth_gen.status_code == 404

    # 2. User A creates a summary
    res_a = await client.post(
        f"/api/v1/documents/{doc_a_id}/summaries?summary_type=brief",
        headers=headers_a,
    )
    assert res_a.status_code == 201
    summary_id = res_a.json()["data"]["id"]

    # 3. User B attempts to list User A's document summaries -> 404
    res_unauth_list = await client.get(
        f"/api/v1/documents/{doc_a_id}/summaries",
        headers=headers_b,
    )
    assert res_unauth_list.status_code == 404

    # 4. User B attempts to view User A's summary -> 404
    res_unauth_get = await client.get(
        f"/api/v1/summaries/{summary_id}",
        headers=headers_b,
    )
    assert res_unauth_get.status_code == 404

    # 5. User B attempts to delete User A's summary -> 404
    res_unauth_del = await client.delete(
        f"/api/v1/summaries/{summary_id}",
        headers=headers_b,
    )
    assert res_unauth_del.status_code == 404

    # Verify summary was NOT deleted
    res_verify = await client.get(
        f"/api/v1/summaries/{summary_id}",
        headers=headers_a,
    )
    assert res_verify.status_code == 200

    # 6. User B cannot poll User A's background job -> 404
    with patch("app.worker.tasks.summary_tasks.generate_summary_task.delay"):
        res_job = await client.post(
            f"/api/v1/documents/{doc_a_id}/summaries?async_mode=true",
            headers=headers_a,
        )
        job_id = res_job.json()["data"]["id"]
        res_unauth_job = await client.get(
            f"/api/v1/jobs/{job_id}",
            headers=headers_b,
        )
        assert res_unauth_job.status_code == 404


@pytest.mark.asyncio
async def test_summaries_crud_and_delete(
    client: AsyncClient,
    setup_environment: dict[str, Any],
) -> None:
    """Verify listing summaries and deleting summaries."""
    env = setup_environment
    headers = {"Authorization": f"Bearer {env['token_a']}"}
    doc_id = env["doc_a"].id

    # Create two summaries
    s1 = await client.post(
        f"/api/v1/documents/{doc_id}/summaries?summary_type=brief",
        headers=headers,
    )
    s2 = await client.post(
        f"/api/v1/documents/{doc_id}/summaries?summary_type=detailed",
        headers=headers,
    )
    assert s1.status_code == 201
    assert s2.status_code == 201

    # List summaries
    list_res = await client.get(
        f"/api/v1/documents/{doc_id}/summaries",
        headers=headers,
    )
    assert list_res.status_code == 200
    items = list_res.json()["data"]
    assert len(items) == 2

    # Delete one summary
    del_id = items[0]["id"]
    del_res = await client.delete(f"/api/v1/summaries/{del_id}", headers=headers)
    assert del_res.status_code == 200
    assert del_res.json()["success"] is True

    # Subsequent GET returns 404
    get_res = await client.get(f"/api/v1/summaries/{del_id}", headers=headers)
    assert get_res.status_code == 404


@pytest.mark.asyncio
async def test_empty_document_handling(
    client: AsyncClient,
    setup_environment: dict[str, Any],
) -> None:
    """Verify that summarizing a document without chunks returns 400 Bad Request."""
    env = setup_environment
    fake_doc_repo: FakeDocumentRepository = env["fake_doc_repo"]
    headers = {"Authorization": f"Bearer {env['token_a']}"}

    # Create empty doc with 0 chunks
    empty_doc = await fake_doc_repo.create_doc(
        user_id=env["user_a"].id,
        title="Empty Slide Deck",
        status="pending",
    )

    res = await client.post(
        f"/api/v1/documents/{empty_doc.id}/summaries",
        headers=headers,
    )
    assert res.status_code == 400
    assert "no text content available" in res.json()["error"]["message"]


@pytest.mark.asyncio
async def test_quota_and_circuit_breaker_errors(
    client: AsyncClient,
    setup_environment: dict[str, Any],
) -> None:
    """Verify proper HTTP status codes for quota and Gemini circuit breaker errors."""
    env = setup_environment
    headers = {"Authorization": f"Bearer {env['token_a']}"}
    doc_id = env["doc_a"].id
    fake_quota: FakeQuotaService = env["fake_quota"]
    fake_gemini: FakeGeminiAdapter = env["fake_gemini"]

    # 1. Quota Exceeded -> 429
    fake_quota.is_exhausted = True
    res_quota = await client.post(
        f"/api/v1/documents/{doc_id}/summaries",
        headers=headers,
    )
    assert res_quota.status_code == 429
    fake_quota.is_exhausted = False

    # 2. Redis Down -> 503 fail-closed
    fake_quota.is_redis_down = True
    res_redis = await client.post(
        f"/api/v1/documents/{doc_id}/summaries",
        headers=headers,
    )
    assert res_redis.status_code == 503
    fake_quota.is_redis_down = False

    # 3. Gemini Rate Limit -> 429
    fake_gemini.simulated_exception = GeminiRateLimitError("429 Rate Limit")
    res_gemini_rl = await client.post(
        f"/api/v1/documents/{doc_id}/summaries",
        headers=headers,
    )
    assert res_gemini_rl.status_code == 429

    # 4. Gemini Circuit Breaker Open -> 503
    fake_gemini.simulated_exception = GeminiCircuitOpenError("Circuit Open")
    res_cb = await client.post(
        f"/api/v1/documents/{doc_id}/summaries",
        headers=headers,
    )
    assert res_cb.status_code == 503

    # 5. Gemini Timeout -> 504
    fake_gemini.simulated_exception = GeminiTimeoutError("Generation Timeout")
    res_to = await client.post(
        f"/api/v1/documents/{doc_id}/summaries",
        headers=headers,
    )
    assert res_to.status_code == 504
    fake_gemini.simulated_exception = None


def test_celery_generate_summary_task_execution() -> None:
    """Verify Celery background task happy and failure execution paths."""
    job_id = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    mock_summary_dto = MagicMock()
    mock_summary_dto.id = uuid.uuid4()

    # 1. Happy Path
    with patch("app.worker.tasks.summary_tasks.async_session_factory") as mock_factory, \
         patch("app.worker.tasks.summary_tasks.SummaryService") as mock_service_cls, \
         patch("app.worker.tasks.summary_tasks.JobRepository") as mock_job_repo_cls, \
         patch("app.worker.idempotency.get_idempotency_guard") as mock_guard:

        mock_session = AsyncMock()
        mock_factory.return_value.__aenter__.return_value = mock_session

        mock_service = AsyncMock()
        mock_service.generate_summary.return_value = mock_summary_dto
        mock_service_cls.return_value = mock_service

        mock_job_repo = AsyncMock()
        mock_job_repo_cls.return_value = mock_job_repo

        guard_inst = MagicMock()
        guard_inst.acquire.return_value = True
        mock_guard.return_value = guard_inst

        result = generate_summary_task(job_id, doc_id, user_id, "general", "free")
        assert result["job_id"] == job_id
        assert result["status"] == "completed"
        assert result["summary_id"] == str(mock_summary_dto.id)

        # Verify job status updates: running (20%) then completed (100%)
        assert mock_job_repo.update_job_status.call_count == 2
        calls = mock_job_repo.update_job_status.call_args_list
        assert calls[0].kwargs["status"] == "running"
        assert calls[0].kwargs["progress_percent"] == 20
        assert calls[1].kwargs["status"] == "completed"
        assert calls[1].kwargs["progress_percent"] == 100

    # 2. Failure Path
    with patch("app.worker.tasks.summary_tasks.async_session_factory") as mock_factory, \
         patch("app.worker.tasks.summary_tasks.SummaryService") as mock_service_cls, \
         patch("app.worker.tasks.summary_tasks.JobRepository") as mock_job_repo_cls, \
         patch("app.worker.idempotency.get_idempotency_guard") as mock_guard:

        mock_session = AsyncMock()
        mock_factory.return_value.__aenter__.return_value = mock_session

        mock_service = AsyncMock()
        mock_service.generate_summary.side_effect = RuntimeError("Generation failed")
        mock_service_cls.return_value = mock_service

        mock_job_repo = AsyncMock()
        mock_job_repo_cls.return_value = mock_job_repo

        guard_inst = MagicMock()
        guard_inst.acquire.return_value = True
        mock_guard.return_value = guard_inst

        with pytest.raises(RuntimeError, match="Generation failed"):
            generate_summary_task(job_id, doc_id, user_id, "general", "free")

        # Verify job marked failed with error code and message
        fail_calls = [
            c for c in mock_job_repo.update_job_status.call_args_list
            if c.kwargs.get("status") == "failed"
        ]
        assert len(fail_calls) == 1
        assert fail_calls[0].kwargs["error_code"] == "RuntimeError"
        assert "Generation failed" in fail_calls[0].kwargs["error_message"]
