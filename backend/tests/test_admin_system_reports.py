"""Comprehensive test suite for Admin System Storage & Resource Consumption Reports (US 2.24 / TASK-M6-06).

Adheres strictly to:
- Invariant 2: Thin router -> Service -> Repository layering.
- Invariant 6: Strict RBAC on admin endpoints (401 unauthenticated, 403 student, 200 admin).
- Standard envelope: {"success": true, "data": ..., "meta": ..., "message": ...}.
- Parameter validation: Query parameter boundaries for limit (1..100) and days (1..365).
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.admin.system import router as admin_system_router
from app.core.security import create_access_token, hash_password
from app.main import app
from app.repositories.ai_usage_repo import (
    AIUsageRepository,
    get_ai_usage_repo,
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
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.admin_service import (
    AdminSystemService,
    get_admin_system_service,
)

# Ensure admin_system router is mounted for standalone test runs
if not any(
    getattr(route, "path", "").startswith("/api/v1/admin/system")
    for route in app.routes
):
    app.include_router(admin_system_router)


# ---------------------------------------------------------------------------
# Test Doubles (In-Memory Fake Repositories)
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for authentication and admin checks."""

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
            "admin": Role(
                id=uuid.uuid4(),
                name="admin",
                description="Administrator role.",
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
        is_active: bool = True,
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

        now = datetime.now(UTC)
        user = User(
            id=user_id,
            email=email.lower().strip(),
            password_hash=password_hash,
            current_plan_code=current_plan_code,
            is_active=is_active,
            is_email_verified=is_email_verified,
            created_at=now,
            updated_at=now,
        )
        user.roles = [role]
        profile = UserProfile(
            id=uuid.uuid4(),
            user_id=user_id,
            full_name=full_name,
            created_at=now,
            updated_at=now,
        )
        profile.user = user
        user.profile = profile

        self.users[user_id] = user
        return user

    async def get_active_users_count(self) -> int:
        return sum(1 for u in self.users.values() if u.is_active)

    async def get_total_users_count(self) -> int:
        return len(self.users)


class FakeDocumentRepository(DocumentRepository):
    """In-memory DocumentRepository double for storage reporting."""

    def __init__(self, user_repo: FakeUserRepository) -> None:
        self.user_repo = user_repo
        self.documents: list[Document] = []

    def add_document(
        self,
        user_id: uuid.UUID,
        file_size_bytes: int,
        file_type: str = "application/pdf",
        status: str = "embedded",
        title: str = "Sample Document",
    ) -> Document:
        now = datetime.now(UTC)
        doc = Document(
            id=uuid.uuid4(),
            user_id=user_id,
            title=title,
            original_file_name=f"{title}.pdf",
            storage_object_key=f"uploads/{uuid.uuid4()}",
            file_type=file_type,
            file_size_bytes=file_size_bytes,
            status=status,
            total_chunks=5,
            chunks_processed=5 if status == "embedded" else 0,
            created_at=now,
            updated_at=now,
        )
        self.documents.append(doc)
        return doc

    async def get_system_storage_stats(self) -> dict[str, Any]:
        total_docs = len(self.documents)
        total_bytes = sum(d.file_size_bytes for d in self.documents)
        average_bytes = round(total_bytes / total_docs, 2) if total_docs > 0 else 0.0

        mime_counts: dict[str, dict[str, int]] = {}
        for d in self.documents:
            if d.file_type not in mime_counts:
                mime_counts[d.file_type] = {"count": 0, "total_bytes": 0}
            mime_counts[d.file_type]["count"] += 1
            mime_counts[d.file_type]["total_bytes"] += d.file_size_bytes

        by_mime_type = [
            {"mime_type": k, "count": v["count"], "total_bytes": v["total_bytes"]}
            for k, v in sorted(mime_counts.items(), key=lambda x: x[1]["total_bytes"], reverse=True)
        ]

        status_counts: dict[str, int] = {}
        for d in self.documents:
            status_counts[d.status] = status_counts.get(d.status, 0) + 1

        by_status = [
            {"status": k, "count": v}
            for k, v in sorted(status_counts.items(), key=lambda x: x[1], reverse=True)
        ]

        return {
            "total_documents": total_docs,
            "total_bytes": total_bytes,
            "average_bytes": average_bytes,
            "by_mime_type": by_mime_type,
            "by_status": by_status,
        }

    async def get_top_storage_users(self, limit: int = 10) -> list[dict[str, Any]]:
        clamped_limit = max(1, min(limit, 100))
        user_storage: dict[uuid.UUID, dict[str, Any]] = {}

        for d in self.documents:
            if d.user_id not in user_storage:
                user = self.user_repo.users.get(d.user_id)
                full_name = user.profile.full_name if user and user.profile else (user.email if user else "Unknown")
                email = user.email if user else "unknown@test.com"
                user_storage[d.user_id] = {
                    "user_id": d.user_id,
                    "email": email,
                    "full_name": full_name,
                    "document_count": 0,
                    "total_bytes": 0,
                }
            user_storage[d.user_id]["document_count"] += 1
            user_storage[d.user_id]["total_bytes"] += d.file_size_bytes

        sorted_users = sorted(
            user_storage.values(),
            key=lambda x: x["total_bytes"],
            reverse=True,
        )
        return sorted_users[:clamped_limit]


class FakeDocumentChunkRepository(DocumentChunkRepository):
    """In-memory DocumentChunkRepository double for chunk reporting."""

    def __init__(self) -> None:
        self.chunks: list[DocumentChunk] = []

    def add_chunk(
        self,
        document_id: uuid.UUID,
        has_embedding: bool = True,
        content: str = "Test chunk content",
    ) -> DocumentChunk:
        now = datetime.now(UTC)
        chunk = DocumentChunk(
            id=uuid.uuid4(),
            document_id=document_id,
            chunk_index=len(self.chunks),
            content=content,
            embedding=[0.1] * 768 if has_embedding else None,  # type: ignore[arg-type]
            created_at=now,
            updated_at=now,
        )
        self.chunks.append(chunk)
        return chunk

    async def get_system_chunk_stats(self) -> dict[str, int]:
        total_chunks = len(self.chunks)
        embedded_chunks = sum(1 for c in self.chunks if c.embedding is not None)
        return {
            "total_chunks": total_chunks,
            "embedded_chunks": embedded_chunks,
        }


class FakeJobRepository(JobRepository):
    """In-memory JobRepository double for Celery job execution telemetry."""

    def __init__(self) -> None:
        self.jobs: list[BackgroundJob] = []

    def add_job(
        self,
        job_type: str,
        status: str = "succeeded",
        created_at: datetime | None = None,
        user_id: uuid.UUID | None = None,
    ) -> BackgroundJob:
        now = created_at or datetime.now(UTC)
        job = BackgroundJob(
            id=uuid.uuid4(),
            user_id=user_id or uuid.uuid4(),
            job_type=job_type,
            status=status,
            progress=100 if status in ("succeeded", "completed") else 0,
            idempotency_key=str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
        )
        self.jobs.append(job)
        return job

    async def get_system_job_stats(self, days: int = 30) -> dict[str, Any]:
        clamped_days = max(1, min(days, 365))
        cutoff = datetime.now(UTC) - timedelta(days=clamped_days)

        filtered = [j for j in self.jobs if j.created_at >= cutoff]
        total_jobs = len(filtered)

        task_counts: dict[str, int] = {}
        for j in filtered:
            task_counts[j.job_type] = task_counts.get(j.job_type, 0) + 1

        by_task_type = [
            {"task_type": k, "count": v}
            for k, v in sorted(task_counts.items(), key=lambda x: x[1], reverse=True)
        ]

        status_counts: dict[str, int] = {}
        for j in filtered:
            status_counts[j.status] = status_counts.get(j.status, 0) + 1

        completed_jobs = status_counts.get("succeeded", 0) + status_counts.get("completed", 0)
        failed_jobs = status_counts.get("failed", 0)
        running_jobs = status_counts.get("processing", 0) + status_counts.get("running", 0)
        queued_jobs = status_counts.get("queued", 0) + status_counts.get("pending", 0)
        cancelled_jobs = status_counts.get("cancelled", 0)

        failure_rate = (
            round((failed_jobs / total_jobs) * 100.0, 2) if total_jobs > 0 else 0.0
        )

        status_map_api = {
            "processing": "running",
            "succeeded": "completed",
            "queued": "queued",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        by_status = [
            {"status": status_map_api.get(k, k), "count": v}
            for k, v in sorted(status_counts.items(), key=lambda x: x[1], reverse=True)
        ]

        return {
            "days": clamped_days,
            "total_jobs": total_jobs,
            "completed_jobs": completed_jobs,
            "failed_jobs": failed_jobs,
            "running_jobs": running_jobs,
            "queued_jobs": queued_jobs,
            "cancelled_jobs": cancelled_jobs,
            "failure_rate": failure_rate,
            "by_task_type": by_task_type,
            "by_status": by_status,
        }

    async def get_total_jobs_count(self) -> int:
        return len(self.jobs)


class FakeAIUsageRepository(AIUsageRepository):
    """In-memory AIUsageRepository double for AI request metrics."""

    def __init__(self) -> None:
        self.request_count: int = 0

    async def get_total_requests_count(self) -> int:
        return self.request_count


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_user_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def fake_document_repo(fake_user_repo: FakeUserRepository) -> FakeDocumentRepository:
    return FakeDocumentRepository(user_repo=fake_user_repo)


@pytest.fixture
def fake_chunk_repo() -> FakeDocumentChunkRepository:
    return FakeDocumentChunkRepository()


@pytest.fixture
def fake_job_repo() -> FakeJobRepository:
    return FakeJobRepository()


@pytest.fixture
def fake_ai_usage_repo() -> FakeAIUsageRepository:
    return FakeAIUsageRepository()


@pytest.fixture
def admin_system_service(
    fake_document_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    fake_job_repo: FakeJobRepository,
    fake_user_repo: FakeUserRepository,
    fake_ai_usage_repo: FakeAIUsageRepository,
) -> AdminSystemService:
    return AdminSystemService(
        document_repo=fake_document_repo,
        chunk_repo=fake_chunk_repo,
        job_repo=fake_job_repo,
        user_repo=fake_user_repo,
        ai_usage_repo=fake_ai_usage_repo,
    )


@pytest_asyncio.fixture
async def admin_user(fake_user_repo: FakeUserRepository) -> User:
    password_hash = hash_password("AdminSecurePass123!")
    return await fake_user_repo.create_user(
        email="admin@edusense.edu.vn",
        password_hash=password_hash,
        full_name="Admin Administrator",
        role_name="admin",
    )


@pytest_asyncio.fixture
async def student_user(fake_user_repo: FakeUserRepository) -> User:
    password_hash = hash_password("StudentPass123!")
    return await fake_user_repo.create_user(
        email="student.john@university.edu",
        password_hash=password_hash,
        full_name="John Student",
        role_name="student",
    )


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_document_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    fake_job_repo: FakeJobRepository,
    fake_ai_usage_repo: FakeAIUsageRepository,
    admin_system_service: AdminSystemService,
) -> AsyncGenerator[AsyncClient, None]:
    """Test client with mock dependency overrides."""
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_document_repo] = lambda: fake_document_repo
    app.dependency_overrides[get_chunk_repo] = lambda: fake_chunk_repo
    app.dependency_overrides[get_job_repo] = lambda: fake_job_repo
    app.dependency_overrides[get_ai_usage_repo] = lambda: fake_ai_usage_repo
    app.dependency_overrides[get_admin_system_service] = lambda: admin_system_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(hours=1),
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. RBAC Security Tests (Invariant 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overview_unauthenticated_returns_401(client: AsyncClient) -> None:
    """Unauthenticated requests to /api/v1/admin/system/overview MUST return 401."""
    resp = await client.get("/api/v1/admin/system/overview")
    assert resp.status_code == 401
    assert resp.json()["success"] is False


@pytest.mark.asyncio
async def test_overview_student_forbidden_returns_403(
    client: AsyncClient, student_user: User
) -> None:
    """Students accessing /api/v1/admin/system/overview MUST receive 403 Forbidden."""
    headers = make_auth_headers(student_user.id)
    resp = await client.get("/api/v1/admin/system/overview", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["success"] is False


@pytest.mark.asyncio
async def test_storage_unauthenticated_returns_401(client: AsyncClient) -> None:
    """Unauthenticated requests to /api/v1/admin/system/storage MUST return 401."""
    resp = await client.get("/api/v1/admin/system/storage")
    assert resp.status_code == 401
    assert resp.json()["success"] is False


@pytest.mark.asyncio
async def test_storage_student_forbidden_returns_403(
    client: AsyncClient, student_user: User
) -> None:
    """Students accessing /api/v1/admin/system/storage MUST receive 403 Forbidden."""
    headers = make_auth_headers(student_user.id)
    resp = await client.get("/api/v1/admin/system/storage", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["success"] is False


@pytest.mark.asyncio
async def test_storage_top_users_student_forbidden_returns_403(
    client: AsyncClient, student_user: User
) -> None:
    """Students accessing /api/v1/admin/system/storage/top-users MUST receive 403 Forbidden."""
    headers = make_auth_headers(student_user.id)
    resp = await client.get("/api/v1/admin/system/storage/top-users", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["success"] is False


@pytest.mark.asyncio
async def test_jobs_unauthenticated_returns_401(client: AsyncClient) -> None:
    """Unauthenticated requests to /api/v1/admin/system/jobs MUST return 401."""
    resp = await client.get("/api/v1/admin/system/jobs")
    assert resp.status_code == 401
    assert resp.json()["success"] is False


@pytest.mark.asyncio
async def test_jobs_student_forbidden_returns_403(
    client: AsyncClient, student_user: User
) -> None:
    """Students accessing /api/v1/admin/system/jobs MUST receive 403 Forbidden."""
    headers = make_auth_headers(student_user.id)
    resp = await client.get("/api/v1/admin/system/jobs", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["success"] is False


# ---------------------------------------------------------------------------
# 2. System Overview Endpoint Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_overview_admin_success(
    client: AsyncClient,
    admin_user: User,
    fake_user_repo: FakeUserRepository,
    fake_document_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    fake_job_repo: FakeJobRepository,
    fake_ai_usage_repo: FakeAIUsageRepository,
) -> None:
    """Admin receives valid envelope with aggregated system-wide counts."""
    # Seed active student and inactive student
    student1 = await fake_user_repo.create_user(
        email="s1@edu.vn",
        password_hash="pwd",
        full_name="Student One",
        is_active=True,
    )
    await fake_user_repo.create_user(
        email="s2@edu.vn",
        password_hash="pwd",
        full_name="Student Two",
        is_active=False,
    )

    # Seed documents
    doc1 = fake_document_repo.add_document(user_id=student1.id, file_size_bytes=1048576)
    doc2 = fake_document_repo.add_document(user_id=student1.id, file_size_bytes=2097152)

    # Seed chunks
    fake_chunk_repo.add_chunk(document_id=doc1.id, has_embedding=True)
    fake_chunk_repo.add_chunk(document_id=doc2.id, has_embedding=True)
    fake_chunk_repo.add_chunk(document_id=doc2.id, has_embedding=False)

    # Seed jobs
    fake_job_repo.add_job(job_type="document_summarization", status="succeeded")
    fake_job_repo.add_job(job_type="question_generation", status="processing")

    # Seed AI usage
    fake_ai_usage_repo.request_count = 42

    headers = make_auth_headers(admin_user.id)
    resp = await client.get("/api/v1/admin/system/overview", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "data" in body
    data = body["data"]

    # Total users: admin + student1 + student2 = 3
    assert data["total_users"] == 3
    # Active users: admin + student1 = 2
    assert data["active_users"] == 2
    assert data["total_documents"] == 2
    assert data["total_bytes"] == 3145728
    assert data["total_chunks"] == 3
    assert data["embedded_chunks"] == 2
    assert data["total_ai_requests"] == 42
    assert data["total_jobs"] == 2
    assert "generated_at" in body["meta"]


# ---------------------------------------------------------------------------
# 3. System Storage Report Endpoint Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_storage_report_admin_success(
    client: AsyncClient,
    admin_user: User,
    fake_document_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
) -> None:
    """Admin receives storage report with MIME and status breakdown and chunk counts."""
    u_id = uuid.uuid4()
    d1 = fake_document_repo.add_document(
        user_id=u_id,
        file_size_bytes=1000,
        file_type="application/pdf",
        status="embedded",
    )
    d2 = fake_document_repo.add_document(
        user_id=u_id,
        file_size_bytes=2000,
        file_type="application/pdf",
        status="embedded",
    )
    fake_document_repo.add_document(
        user_id=u_id,
        file_size_bytes=3000,
        file_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        status="processing",
    )

    fake_chunk_repo.add_chunk(document_id=d1.id, has_embedding=True)
    fake_chunk_repo.add_chunk(document_id=d2.id, has_embedding=True)

    headers = make_auth_headers(admin_user.id)
    resp = await client.get("/api/v1/admin/system/storage", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]

    assert data["total_documents"] == 3
    assert data["total_bytes"] == 6000
    assert data["average_bytes"] == 2000.0
    assert data["total_chunks"] == 2
    assert data["embedded_chunks"] == 2

    mime_types = {item["mime_type"]: item for item in data["by_mime_type"]}
    assert "application/pdf" in mime_types
    assert mime_types["application/pdf"]["count"] == 2
    assert mime_types["application/pdf"]["total_bytes"] == 3000

    statuses = {item["status"]: item["count"] for item in data["by_status"]}
    assert statuses["embedded"] == 2
    assert statuses["processing"] == 1


# ---------------------------------------------------------------------------
# 4. Top Storage Users Endpoint Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_storage_users_admin_ranked(
    client: AsyncClient,
    admin_user: User,
    fake_user_repo: FakeUserRepository,
    fake_document_repo: FakeDocumentRepository,
) -> None:
    """Top storage users endpoint returns students sorted by storage volume descending."""
    user_heavy = await fake_user_repo.create_user(
        email="heavy@edu.vn",
        password_hash="pwd",
        full_name="Heavy Uploader",
    )
    user_light = await fake_user_repo.create_user(
        email="light@edu.vn",
        password_hash="pwd",
        full_name="Light Uploader",
    )

    fake_document_repo.add_document(user_id=user_heavy.id, file_size_bytes=50_000_000)
    fake_document_repo.add_document(user_id=user_heavy.id, file_size_bytes=20_000_000)
    fake_document_repo.add_document(user_id=user_light.id, file_size_bytes=5_000_000)

    headers = make_auth_headers(admin_user.id)
    resp = await client.get("/api/v1/admin/system/storage/top-users?limit=5", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]

    assert len(data) == 2
    # Heavy uploader should be first
    assert data[0]["user_id"] == str(user_heavy.id)
    assert data[0]["total_bytes"] == 70_000_000
    assert data[0]["document_count"] == 2
    assert data[0]["full_name"] == "Heavy Uploader"
    assert data[0]["email"] == "heavy@edu.vn"

    # Light uploader should be second
    assert data[1]["user_id"] == str(user_light.id)
    assert data[1]["total_bytes"] == 5_000_000
    assert data[1]["document_count"] == 1

    assert body["meta"]["limit"] == 5
    assert body["meta"]["total_returned"] == 2


@pytest.mark.asyncio
async def test_top_storage_users_validation_error_on_invalid_limit(
    client: AsyncClient, admin_user: User
) -> None:
    """Invalid limit (< 1 or > 100) returns 422 Unprocessable Content."""
    headers = make_auth_headers(admin_user.id)

    # limit = 0
    resp0 = await client.get("/api/v1/admin/system/storage/top-users?limit=0", headers=headers)
    assert resp0.status_code == 422
    assert resp0.json()["success"] is False

    # limit = 101
    resp101 = await client.get("/api/v1/admin/system/storage/top-users?limit=101", headers=headers)
    assert resp101.status_code == 422
    assert resp101.json()["success"] is False


# ---------------------------------------------------------------------------
# 5. Background Jobs Telemetry Endpoint Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_jobs_report_admin_success(
    client: AsyncClient,
    admin_user: User,
    fake_job_repo: FakeJobRepository,
) -> None:
    """Admin receives background job telemetry with status, failure rate, and worker status."""
    now = datetime.now(UTC)

    # 3 succeeded, 1 failed, 1 processing
    fake_job_repo.add_job("document_summarization", status="succeeded", created_at=now)
    fake_job_repo.add_job("document_summarization", status="succeeded", created_at=now)
    fake_job_repo.add_job("question_generation", status="succeeded", created_at=now)
    fake_job_repo.add_job("question_generation", status="failed", created_at=now)
    fake_job_repo.add_job("document_indexing", status="processing", created_at=now)

    # Job older than 30 days should be excluded
    fake_job_repo.add_job(
        "old_task",
        status="succeeded",
        created_at=now - timedelta(days=35),
    )

    headers = make_auth_headers(admin_user.id)
    resp = await client.get("/api/v1/admin/system/jobs?days=30", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]

    assert data["days"] == 30
    assert data["total_jobs"] == 5
    assert data["completed_jobs"] == 3
    assert data["failed_jobs"] == 1
    assert data["running_jobs"] == 1
    # failure_rate = 1 / 5 = 20.0%
    assert data["failure_rate"] == 20.0
    # 20.0% failure rate should trigger "degraded" (>= 15% and < 50%)
    assert data["worker_status"] == "degraded"

    task_types = {t["task_type"]: t["count"] for t in data["by_task_type"]}
    assert task_types["document_summarization"] == 2
    assert task_types["question_generation"] == 2
    assert task_types["document_indexing"] == 1

    status_counts = {s["status"]: s["count"] for s in data["by_status"]}
    assert status_counts["completed"] == 3
    assert status_counts["failed"] == 1
    assert status_counts["running"] == 1


@pytest.mark.asyncio
async def test_system_jobs_worker_health_states(
    client: AsyncClient,
    admin_user: User,
    fake_job_repo: FakeJobRepository,
) -> None:
    """Verify worker health status calculations across idle, healthy, degraded, critical."""
    headers = make_auth_headers(admin_user.id)

    # 1. Idle (0 jobs)
    resp_idle = await client.get("/api/v1/admin/system/jobs", headers=headers)
    assert resp_idle.json()["data"]["worker_status"] == "idle"
    assert resp_idle.json()["data"]["failure_rate"] == 0.0

    # 2. Healthy (100% success)
    fake_job_repo.add_job("task", status="succeeded")
    resp_healthy = await client.get("/api/v1/admin/system/jobs", headers=headers)
    assert resp_healthy.json()["data"]["worker_status"] == "healthy"
    assert resp_healthy.json()["data"]["failure_rate"] == 0.0

    # 3. Critical (>= 50% failures)
    fake_job_repo.add_job("task", status="failed")
    fake_job_repo.add_job("task", status="failed")
    # Now: 1 succeeded, 2 failed -> 66.67% failure rate
    resp_crit = await client.get("/api/v1/admin/system/jobs", headers=headers)
    assert resp_crit.json()["data"]["worker_status"] == "critical"
    assert resp_crit.json()["data"]["failure_rate"] == 66.67


@pytest.mark.asyncio
async def test_system_jobs_validation_error_on_invalid_days(
    client: AsyncClient, admin_user: User
) -> None:
    """Invalid days (< 1 or > 365) returns 422 Unprocessable Content."""
    headers = make_auth_headers(admin_user.id)

    # days = 0
    resp0 = await client.get("/api/v1/admin/system/jobs?days=0", headers=headers)
    assert resp0.status_code == 422
    assert resp0.json()["success"] is False

    # days = 500
    resp500 = await client.get("/api/v1/admin/system/jobs?days=500", headers=headers)
    assert resp500.status_code == 422
    assert resp500.json()["success"] is False


# ---------------------------------------------------------------------------
# 6. Empty System State Handling Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_system_overview_and_storage(
    client: AsyncClient,
    admin_user: User,
) -> None:
    """Ensure zero division does not occur on fresh systems with no documents or chunks."""
    headers = make_auth_headers(admin_user.id)

    resp_overview = await client.get("/api/v1/admin/system/overview", headers=headers)
    assert resp_overview.status_code == 200
    assert resp_overview.json()["data"]["total_documents"] == 0
    assert resp_overview.json()["data"]["total_bytes"] == 0

    resp_storage = await client.get("/api/v1/admin/system/storage", headers=headers)
    assert resp_storage.status_code == 200
    data_storage = resp_storage.json()["data"]
    assert data_storage["total_documents"] == 0
    assert data_storage["total_bytes"] == 0
    assert data_storage["average_bytes"] == 0.0
    assert data_storage["by_mime_type"] == []
    assert data_storage["by_status"] == []

    resp_top_users = await client.get("/api/v1/admin/system/storage/top-users", headers=headers)
    assert resp_top_users.status_code == 200
    assert resp_top_users.json()["data"] == []
