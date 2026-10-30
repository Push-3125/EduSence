"""Comprehensive tests for study documents repository, service, and API endpoints (US 2.10)."""

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure backend root is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.documents import router as documents_router
from app.clients.storage_client import MinIOStorageClient, get_storage_client
from app.core.security import create_access_token, hash_password
from app.main import app
from app.repositories.course_repo import Course, CourseRepository, get_course_repo
from app.repositories.document_repo import (
    Document,
    DocumentRepository,
    get_document_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.course_service import CourseNotFoundError
from app.services.document_service import (
    MAX_FILE_SIZE_BYTES,
    DocumentNotFoundError,
    DocumentService,
    DocumentValidationError,
    FileTooLargeError,
    UnsupportedFileTypeError,
    get_document_service,
    validate_file_type,
)

# Ensure documents router is registered
if not any(getattr(route, "path", "").startswith("/api/v1/documents") for route in app.routes):
    app.include_router(documents_router)


# ---------------------------------------------------------------------------
# Test Doubles: In-Memory Fake Repositories & Storage Client
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
        user.roles = [role]
        user.profile = UserProfile(
            user_id=user_id,
            full_name=full_name.strip(),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.users[user_id] = user
        return user


class FakeCourseRepository(CourseRepository):
    """In-memory CourseRepository double for course ownership verification."""

    def __init__(self) -> None:
        self.courses: dict[uuid.UUID, Course] = {}

    async def create(
        self,
        user_id: uuid.UUID,
        name: str,
        course_code: str | None = None,
        description: str | None = None,
        instructor: str | None = None,
        semester: str | None = None,
        academic_year: str | None = None,
        status: str = "active",
    ) -> Course:
        course_id = uuid.uuid4()
        now = datetime.now(UTC)
        course = Course(
            id=course_id,
            user_id=user_id,
            name=name.strip(),
            course_code=course_code.strip() if course_code else None,
            description=description.strip() if description else None,
            instructor=instructor.strip() if instructor else None,
            semester=semester.strip() if semester else None,
            academic_year=academic_year.strip() if academic_year else None,
            status=status,
            created_at=now,
            updated_at=now,
        )
        self.courses[course_id] = course
        return course

    async def get_by_id_and_user(
        self,
        course_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Course | None:
        course = self.courses.get(course_id)
        if course and course.user_id == user_id:
            return course
        return None


class FakeDocumentRepository(DocumentRepository):
    """In-memory DocumentRepository double for study document tests."""

    def __init__(self) -> None:
        self.documents: dict[uuid.UUID, Document] = {}

    async def create(
        self,
        user_id: uuid.UUID,
        title: str,
        original_file_name: str,
        storage_object_key: str,
        file_type: str,
        file_size_bytes: int,
        course_id: uuid.UUID | None = None,
        page_count: int | None = None,
        status: str = "pending",
        id: uuid.UUID | None = None,
    ) -> Document:
        doc_id = id or uuid.uuid4()
        now = datetime.now(UTC)
        doc = Document(
            id=doc_id,
            user_id=user_id,
            course_id=course_id,
            title=title.strip(),
            original_file_name=original_file_name.strip(),
            storage_object_key=storage_object_key.strip(),
            file_type=file_type.strip(),
            file_size_bytes=file_size_bytes,
            page_count=page_count,
            status=status,
            total_chunks=0,
            chunks_processed=0,
            processing_error_code=None,
            created_at=now,
            updated_at=now,
        )
        self.documents[doc_id] = doc
        return doc

    async def get_by_id_and_user(
        self,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Document | None:
        doc = self.documents.get(document_id)
        if doc and doc.user_id == user_id:
            return doc
        return None

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        status: str | None = None,
        search_query: str | None = None,
        file_type: str | None = None,
        tag: str | None = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Document], int]:
        matching = [d for d in self.documents.values() if d.user_id == user_id]
        if course_id is not None:
            matching = [d for d in matching if d.course_id == course_id]
        if status is not None:
            matching = [d for d in matching if d.status == status]
        if search_query and search_query.strip():
            sq = search_query.strip().lower()
            matching = [
                d for d in matching
                if sq in d.title.lower() or sq in d.original_file_name.lower()
            ]
        if file_type and file_type.strip():
            ft = file_type.strip().lower()
            matching = [d for d in matching if ft in d.file_type.lower()]

        sort_key_map = {
            "title": lambda x: x.title.lower(),
            "created_at": lambda x: x.created_at,
            "updated_at": lambda x: x.updated_at,
            "file_size_bytes": lambda x: x.file_size_bytes,
        }
        key_fn = sort_key_map.get(sort_by, lambda x: x.created_at)
        matching.sort(key=key_fn, reverse=(sort_order.lower() != "asc"))
        total = len(matching)
        items = matching[skip : skip + limit]
        return items, total

    async def update(self, document: Document, **kwargs: Any) -> Document:
        allowed = {
            "title",
            "course_id",
            "page_count",
            "status",
            "total_chunks",
            "chunks_processed",
            "processing_error_code",
        }
        for k, v in kwargs.items():
            if k in allowed:
                setattr(document, k, v)
        document.updated_at = datetime.now(UTC)
        return document

    async def delete(self, document: Document) -> None:
        self.documents.pop(document.id, None)


class FakeStorageClient(MinIOStorageClient):
    """In-memory MinIOStorageClient test double."""

    def __init__(self) -> None:
        self.stored_files: dict[str, dict[str, Any]] = {}

    def upload_file(
        self,
        object_name: str,
        file_data: Any,
        length: int | None = None,
        content_type: str = "application/octet-stream",
        bucket_name: str | None = None,
    ) -> str:
        data = file_data if isinstance(file_data, bytes) else file_data.read()
        self.stored_files[object_name] = {
            "data": data,
            "content_type": content_type,
            "length": length or len(data),
            "bucket": bucket_name or "edusense-documents",
        }
        return f"{bucket_name or 'edusense-documents'}/{object_name}"

    def generate_presigned_download_url(
        self,
        object_name: str,
        expires_delta: timedelta = timedelta(minutes=15),
        bucket_name: str | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> str:
        bucket = bucket_name or "edusense-documents"
        return f"http://minio-test:9000/{bucket}/{object_name}?token=presigned-valid-token"

    def delete_file(self, object_name: str, bucket_name: str | None = None) -> bool:
        self.stored_files.pop(object_name, None)
        return True

    def file_exists(self, object_name: str, bucket_name: str | None = None) -> bool:
        return object_name in self.stored_files


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_user_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def fake_course_repo() -> FakeCourseRepository:
    return FakeCourseRepository()


@pytest.fixture
def fake_doc_repo() -> FakeDocumentRepository:
    return FakeDocumentRepository()


@pytest.fixture
def fake_storage() -> FakeStorageClient:
    return FakeStorageClient()


@pytest.fixture
def document_service(
    fake_doc_repo: FakeDocumentRepository,
    fake_course_repo: FakeCourseRepository,
    fake_storage: FakeStorageClient,
) -> DocumentService:
    return DocumentService(
        document_repo=fake_doc_repo,
        course_repo=fake_course_repo,
        storage_client=fake_storage,
    )


@pytest_asyncio.fixture
async def student_a(fake_user_repo: FakeUserRepository) -> User:
    return await fake_user_repo.create_user(
        email="student.alice@university.edu",
        password_hash=hash_password("ValidPassword123!"),
        full_name="Alice Student",
    )


@pytest_asyncio.fixture
async def student_b(fake_user_repo: FakeUserRepository) -> User:
    return await fake_user_repo.create_user(
        email="student.bob@university.edu",
        password_hash=hash_password("ValidPassword123!"),
        full_name="Bob Student",
    )


@pytest_asyncio.fixture
async def inactive_student(fake_user_repo: FakeUserRepository) -> User:
    user = await fake_user_repo.create_user(
        email="student.inactive@university.edu",
        password_hash=hash_password("ValidPassword123!"),
        full_name="Inactive Student",
    )
    user.is_active = False
    return user


@pytest_asyncio.fixture
async def course_a(fake_course_repo: FakeCourseRepository, student_a: User) -> Course:
    return await fake_course_repo.create(
        user_id=student_a.id,
        name="Operating Systems",
        course_code="CS301",
    )


@pytest_asyncio.fixture
async def course_b(fake_course_repo: FakeCourseRepository, student_b: User) -> Course:
    return await fake_course_repo.create(
        user_id=student_b.id,
        name="Database Systems",
        course_code="CS302",
    )


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_course_repo: FakeCourseRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_storage: FakeStorageClient,
) -> AsyncGenerator[AsyncClient, None]:
    service = DocumentService(
        document_repo=fake_doc_repo,
        course_repo=fake_course_repo,
        storage_client=fake_storage,
    )
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_course_repo] = lambda: fake_course_repo
    app.dependency_overrides[get_document_repo] = lambda: fake_doc_repo
    app.dependency_overrides[get_storage_client] = lambda: fake_storage
    app.dependency_overrides[get_document_service] = lambda: service

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
# Repository Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_create_and_get(fake_doc_repo: FakeDocumentRepository) -> None:
    user_id = uuid.uuid4()
    doc = await fake_doc_repo.create(
        user_id=user_id,
        title="Physics Notes",
        original_file_name="notes.pdf",
        storage_object_key="documents/notes.pdf",
        file_type="application/pdf",
        file_size_bytes=1024,
    )
    assert doc.id is not None
    assert doc.title == "Physics Notes"
    assert doc.status == "pending"

    found = await fake_doc_repo.get_by_id_and_user(doc.id, user_id)
    assert found is not None
    assert found.id == doc.id

    # Foreign user returns None
    other_user = uuid.uuid4()
    assert await fake_doc_repo.get_by_id_and_user(doc.id, other_user) is None


@pytest.mark.asyncio
async def test_repo_list_filter_and_pagination(fake_doc_repo: FakeDocumentRepository) -> None:
    user_id = uuid.uuid4()
    course_id = uuid.uuid4()

    await fake_doc_repo.create(
        user_id=user_id,
        title="Doc 1",
        original_file_name="d1.pdf",
        storage_object_key="key1",
        file_type="application/pdf",
        file_size_bytes=100,
        course_id=course_id,
        status="pending",
    )
    d2 = await fake_doc_repo.create(
        user_id=user_id,
        title="Doc 2",
        original_file_name="d2.pdf",
        storage_object_key="key2",
        file_type="application/pdf",
        file_size_bytes=200,
        course_id=course_id,
        status="embedded",
    )
    await fake_doc_repo.create(
        user_id=user_id,
        title="Doc 3",
        original_file_name="d3.pdf",
        storage_object_key="key3",
        file_type="application/pdf",
        file_size_bytes=300,
        status="pending",
    )

    # Filter by course_id
    course_docs, total = await fake_doc_repo.list_by_user(user_id=user_id, course_id=course_id)
    assert total == 2
    assert len(course_docs) == 2

    # Filter by status
    embedded_docs, total = await fake_doc_repo.list_by_user(user_id=user_id, status="embedded")
    assert total == 1
    assert embedded_docs[0].id == d2.id

    # Pagination
    page1, total = await fake_doc_repo.list_by_user(user_id=user_id, skip=0, limit=2)
    assert total == 3
    assert len(page1) == 2


@pytest.mark.asyncio
async def test_repo_update_and_delete(fake_doc_repo: FakeDocumentRepository) -> None:
    user_id = uuid.uuid4()
    doc = await fake_doc_repo.create(
        user_id=user_id,
        title="Original Title",
        original_file_name="orig.pdf",
        storage_object_key="key",
        file_type="application/pdf",
        file_size_bytes=500,
    )

    updated = await fake_doc_repo.update(doc, title="Updated Title", status="embedded")
    assert updated.title == "Updated Title"
    assert updated.status == "embedded"

    await fake_doc_repo.delete(doc)
    assert await fake_doc_repo.get_by_id_and_user(doc.id, user_id) is None


@pytest.mark.asyncio
async def test_sql_document_repository_crud() -> None:
    from unittest.mock import AsyncMock, MagicMock
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    repo = DocumentRepository(session=mock_session)

    user_id = uuid.uuid4()
    doc = await repo.create(
        user_id=user_id,
        title="Calculus",
        original_file_name="calc.pdf",
        storage_object_key="documents/calc.pdf",
        file_type="application/pdf",
        file_size_bytes=2048,
    )
    assert doc.title == "Calculus"
    mock_session.add.assert_called_once()
    mock_session.flush.assert_called()

    # Test update
    await repo.update(doc, status="processing")
    assert doc.status == "processing"

    # Test delete
    await repo.delete(doc)
    mock_session.delete.assert_called_once_with(doc)


# ---------------------------------------------------------------------------
# Unit Tests: File Type Validation Helper
# ---------------------------------------------------------------------------


def test_validate_file_type_pdf_success() -> None:
    mime = validate_file_type("lecture_notes.pdf", "application/pdf")
    assert mime == "application/pdf"


def test_validate_file_type_docx_success() -> None:
    mime = validate_file_type("assignment.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_validate_file_type_images_success() -> None:
    assert validate_file_type("diagram.png", "image/png") == "image/png"
    assert validate_file_type("whiteboard.jpg", "image/jpeg") == "image/jpeg"
    assert validate_file_type("graph.webp", "image/webp") == "image/webp"


def test_validate_file_type_octet_stream_fallback() -> None:
    # When browser sends generic octet-stream, extension determines MIME type
    mime = validate_file_type("syllabus.pdf", "application/octet-stream")
    assert mime == "application/pdf"


def test_validate_file_type_unsupported_extension() -> None:
    with pytest.raises(UnsupportedFileTypeError) as exc_info:
        validate_file_type("script.exe", "application/x-msdownload")
    assert "Unsupported file extension" in str(exc_info.value)


def test_validate_file_type_unsupported_mime() -> None:
    with pytest.raises(UnsupportedFileTypeError) as exc_info:
        validate_file_type("document.pdf", "application/x-msdownload")
    assert "Unsupported MIME type" in str(exc_info.value)


def test_validate_file_type_extension_mime_mismatch() -> None:
    with pytest.raises(UnsupportedFileTypeError) as exc_info:
        validate_file_type("file.pdf", "image/png")
    assert "does not match" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Service Layer Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_upload_pdf_success(
    document_service: DocumentService,
    fake_storage: FakeStorageClient,
    student_a: User,
    course_a: Course,
) -> None:
    file_payload = ("lecture_1.pdf", b"%PDF-1.4 test pdf binary stream", "application/pdf")
    dto = await document_service.upload_document(
        user_id=student_a.id,
        file=file_payload,
        title="Lecture 1: Introduction",
        course_id=course_a.id,
    )

    assert dto.id is not None
    assert dto.user_id == student_a.id
    assert dto.course_id == course_a.id
    assert dto.title == "Lecture 1: Introduction"
    assert dto.original_file_name == "lecture_1.pdf"
    assert dto.file_type == "application/pdf"
    assert dto.file_size_bytes == len(b"%PDF-1.4 test pdf binary stream")
    assert dto.status == "pending"
    assert dto.storage_object_key.startswith(f"documents/{student_a.id}/{dto.id}/")

    # Verify object uploaded to storage client
    assert fake_storage.file_exists(dto.storage_object_key)


@pytest.mark.asyncio
async def test_service_upload_default_title(
    document_service: DocumentService,
    student_a: User,
) -> None:
    file_payload = ("chapter_notes.docx", b"PK\x03\x04 fake docx bytes", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    dto = await document_service.upload_document(
        user_id=student_a.id,
        file=file_payload,
        title=None,
    )
    assert dto.title == "chapter_notes"


@pytest.mark.asyncio
async def test_service_upload_too_large_fails(
    document_service: DocumentService,
    student_a: User,
) -> None:
    oversized = b"X" * (MAX_FILE_SIZE_BYTES + 100)
    file_payload = ("huge.pdf", oversized, "application/pdf")

    with pytest.raises(FileTooLargeError) as exc_info:
        await document_service.upload_document(
            user_id=student_a.id,
            file=file_payload,
        )
    assert "exceeds maximum allowed limit of 20 MB" in str(exc_info.value)


@pytest.mark.asyncio
async def test_service_upload_empty_file_fails(
    document_service: DocumentService,
    student_a: User,
) -> None:
    file_payload = ("empty.pdf", b"", "application/pdf")
    with pytest.raises(DocumentValidationError) as exc_info:
        await document_service.upload_document(
            user_id=student_a.id,
            file=file_payload,
        )
    assert "cannot be empty" in str(exc_info.value)


@pytest.mark.asyncio
async def test_service_upload_foreign_course_fails(
    document_service: DocumentService,
    student_a: User,
    course_b: Course,  # Owned by student_b
) -> None:
    file_payload = ("lab1.pdf", b"%PDF-1.4 sample", "application/pdf")
    with pytest.raises(CourseNotFoundError):
        await document_service.upload_document(
            user_id=student_a.id,
            file=file_payload,
            course_id=course_b.id,
        )


@pytest.mark.asyncio
async def test_service_get_and_delete_document(
    document_service: DocumentService,
    fake_storage: FakeStorageClient,
    student_a: User,
) -> None:
    file_payload = ("cheatsheet.png", b"\x89PNG\r\n\x1a\n fake image", "image/png")
    created = await document_service.upload_document(
        user_id=student_a.id,
        file=file_payload,
    )

    # Get with download URL
    fetched = await document_service.get_document(
        document_id=created.id,
        user_id=student_a.id,
        generate_download_url=True,
    )
    assert fetched.id == created.id
    assert fetched.download_url is not None
    assert "http://minio-test:9000" in fetched.download_url

    # Delete
    await document_service.delete_document(document_id=created.id, user_id=student_a.id)
    assert not fake_storage.file_exists(created.storage_object_key)

    # Next get raises NotFound
    with pytest.raises(DocumentNotFoundError):
        await document_service.get_document(document_id=created.id, user_id=student_a.id)


# ---------------------------------------------------------------------------
# API Endpoints Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_upload_valid_pdf_success(
    client: AsyncClient,
    student_a: User,
    course_a: Course,
) -> None:
    headers = make_auth_headers(student_a.id)
    files = {
        "file": ("syllabus_week1.pdf", b"%PDF-1.4 dummy valid content", "application/pdf"),
    }
    data = {
        "title": "Week 1 Syllabus",
        "course_id": str(course_a.id),
    }

    resp = await client.post("/api/v1/documents/upload", files=files, data=data, headers=headers)
    assert resp.status_code == 201
    payload = resp.json()
    assert payload["success"] is True
    doc = payload["data"]
    assert doc["title"] == "Week 1 Syllabus"
    assert doc["original_file_name"] == "syllabus_week1.pdf"
    assert doc["file_type"] == "application/pdf"
    assert doc["course_id"] == str(course_a.id)
    assert doc["status"] == "pending"
    assert doc["file_size_bytes"] == len(b"%PDF-1.4 dummy valid content")


@pytest.mark.asyncio
async def test_api_upload_valid_docx_success(client: AsyncClient, student_a: User) -> None:
    headers = make_auth_headers(student_a.id)
    files = {
        "file": ("essay.docx", b"PK\x03\x04 dummy docx bytes", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    }
    resp = await client.post("/api/v1/documents/upload", files=files, headers=headers)
    assert resp.status_code == 201
    payload = resp.json()
    assert payload["success"] is True
    assert payload["data"]["file_type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.asyncio
async def test_api_upload_valid_image_success(client: AsyncClient, student_a: User) -> None:
    headers = make_auth_headers(student_a.id)
    files = {
        "file": ("architecture.png", b"\x89PNG\r\n\x1a\n image bytes", "image/png"),
    }
    resp = await client.post("/api/v1/documents/upload", files=files, headers=headers)
    assert resp.status_code == 201
    payload = resp.json()
    assert payload["success"] is True
    assert payload["data"]["file_type"] == "image/png"


@pytest.mark.asyncio
async def test_api_upload_unsupported_file_type_400(client: AsyncClient, student_a: User) -> None:
    headers = make_auth_headers(student_a.id)
    files = {
        "file": ("malware.exe", b"MZ executable payload", "application/x-msdownload"),
    }
    resp = await client.post("/api/v1/documents/upload", files=files, headers=headers)
    assert resp.status_code == 400
    payload = resp.json()
    assert payload["success"] is False
    assert "Unsupported file" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_api_upload_file_exceeding_20mb_413(client: AsyncClient, student_a: User) -> None:
    headers = make_auth_headers(student_a.id)
    big_content = b"0" * (MAX_FILE_SIZE_BYTES + 1024)
    files = {
        "file": ("oversized.pdf", big_content, "application/pdf"),
    }
    resp = await client.post("/api/v1/documents/upload", files=files, headers=headers)
    assert resp.status_code == 413
    payload = resp.json()
    assert payload["success"] is False
    assert "exceeds maximum allowed limit" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_api_upload_foreign_course_404(
    client: AsyncClient,
    student_a: User,
    course_b: Course,  # Owned by student_b
) -> None:
    headers = make_auth_headers(student_a.id)
    files = {
        "file": ("lecture.pdf", b"%PDF-1.4 file content", "application/pdf"),
    }
    data = {
        "course_id": str(course_b.id),
    }
    resp = await client.post("/api/v1/documents/upload", files=files, data=data, headers=headers)
    assert resp.status_code == 404
    payload = resp.json()
    assert payload["success"] is False
    assert "Course not found" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_api_tenant_isolation_list(
    client: AsyncClient,
    student_a: User,
    student_b: User,
) -> None:
    headers_a = make_auth_headers(student_a.id)
    headers_b = make_auth_headers(student_b.id)

    # Student A uploads 2 documents
    await client.post(
        "/api/v1/documents/upload",
        files={"file": ("doc_a1.pdf", b"%PDF-1.4 test a1", "application/pdf")},
        headers=headers_a,
    )
    await client.post(
        "/api/v1/documents/upload",
        files={"file": ("doc_a2.pdf", b"%PDF-1.4 test a2", "application/pdf")},
        headers=headers_a,
    )

    # Student B uploads 1 document
    await client.post(
        "/api/v1/documents/upload",
        files={"file": ("doc_b1.pdf", b"%PDF-1.4 test b1", "application/pdf")},
        headers=headers_b,
    )

    # Student A lists documents -> sees exactly 2
    resp_a = await client.get("/api/v1/documents", headers=headers_a)
    assert resp_a.status_code == 200
    items_a = resp_a.json()["data"]["items"]
    assert len(items_a) == 2
    for item in items_a:
        assert item["user_id"] == str(student_a.id)

    # Student B lists documents -> sees exactly 1
    resp_b = await client.get("/api/v1/documents", headers=headers_b)
    assert resp_b.status_code == 200
    items_b = resp_b.json()["data"]["items"]
    assert len(items_b) == 1
    assert items_b[0]["user_id"] == str(student_b.id)


@pytest.mark.asyncio
async def test_api_get_document_presigned_url_and_foreign_404(
    client: AsyncClient,
    student_a: User,
    student_b: User,
) -> None:
    headers_a = make_auth_headers(student_a.id)
    headers_b = make_auth_headers(student_b.id)

    # Student A uploads document
    upload_resp = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("private_notes.pdf", b"%PDF-1.4 my secret notes", "application/pdf")},
        headers=headers_a,
    )
    doc_id = upload_resp.json()["data"]["id"]

    # Student A gets document -> 200 OK with presigned download_url
    get_resp_a = await client.get(f"/api/v1/documents/{doc_id}", headers=headers_a)
    assert get_resp_a.status_code == 200
    doc_data = get_resp_a.json()["data"]
    assert doc_data["id"] == doc_id
    assert doc_data["download_url"] is not None
    assert "token=" in doc_data["download_url"]

    # Student B attempts to get Student A's document -> 404
    get_resp_b = await client.get(f"/api/v1/documents/{doc_id}", headers=headers_b)
    assert get_resp_b.status_code == 404


@pytest.mark.asyncio
async def test_api_delete_document_and_foreign_isolation(
    client: AsyncClient,
    fake_storage: FakeStorageClient,
    student_a: User,
    student_b: User,
) -> None:
    headers_a = make_auth_headers(student_a.id)
    headers_b = make_auth_headers(student_b.id)

    # Student A uploads document
    upload_resp = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("to_delete.pdf", b"%PDF-1.4 delete me", "application/pdf")},
        headers=headers_a,
    )
    doc_id = upload_resp.json()["data"]["id"]
    storage_key = upload_resp.json()["data"]["storage_object_key"]
    assert fake_storage.file_exists(storage_key)

    # Student B attempts to delete Student A's document -> 404
    del_foreign = await client.delete(f"/api/v1/documents/{doc_id}", headers=headers_b)
    assert del_foreign.status_code == 404
    assert fake_storage.file_exists(storage_key)

    # Student A deletes own document -> 200 OK
    del_resp = await client.delete(f"/api/v1/documents/{doc_id}", headers=headers_a)
    assert del_resp.status_code == 200
    assert del_resp.json()["success"] is True

    # MinIO object should be removed
    assert not fake_storage.file_exists(storage_key)

    # Subsequent GET returns 404
    get_deleted = await client.get(f"/api/v1/documents/{doc_id}", headers=headers_a)
    assert get_deleted.status_code == 404


@pytest.mark.asyncio
async def test_api_list_with_course_and_status_filters(
    client: AsyncClient,
    student_a: User,
    course_a: Course,
    fake_doc_repo: FakeDocumentRepository,
) -> None:
    headers = make_auth_headers(student_a.id)

    # Create one document linked to course_a
    await client.post(
        "/api/v1/documents/upload",
        files={"file": ("course_doc.pdf", b"%PDF-1.4 course doc", "application/pdf")},
        data={"course_id": str(course_a.id)},
        headers=headers,
    )
    # Create another document without course
    resp2 = await client.post(
        "/api/v1/documents/upload",
        files={"file": ("general_doc.pdf", b"%PDF-1.4 gen doc", "application/pdf")},
        headers=headers,
    )
    doc2_id = uuid.UUID(resp2.json()["data"]["id"])
    # Update second doc status to 'embedded'
    doc2_obj = await fake_doc_repo.get_by_id_and_user(doc2_id, student_a.id)
    assert doc2_obj is not None
    await fake_doc_repo.update(doc2_obj, status="embedded")

    # Filter by course_id
    resp_course = await client.get(f"/api/v1/documents?course_id={course_a.id}", headers=headers)
    assert resp_course.status_code == 200
    assert resp_course.json()["data"]["total"] == 1
    assert resp_course.json()["data"]["items"][0]["course_id"] == str(course_a.id)

    # Filter by status 'embedded'
    resp_embedded = await client.get("/api/v1/documents?status=embedded", headers=headers)
    assert resp_embedded.status_code == 200
    assert resp_embedded.json()["data"]["total"] == 1
    assert resp_embedded.json()["data"]["items"][0]["status"] == "embedded"


@pytest.mark.asyncio
async def test_api_unauthenticated_request_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/documents")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_inactive_user_403(client: AsyncClient, inactive_student: User) -> None:
    headers = make_auth_headers(inactive_student.id)
    resp = await client.get("/api/v1/documents", headers=headers)
    assert resp.status_code == 403
