"""Comprehensive tests for document search, filtering, and tag management API (US 2.11)."""

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# Ensure backend root is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.documents import router as documents_router
from app.api.v1.tags import router as tags_router
from app.clients.storage_client import MinIOStorageClient, get_storage_client
from app.core.security import create_access_token, hash_password
from app.main import app
from app.repositories.course_repo import Course, CourseRepository, get_course_repo
from app.repositories.document_repo import (
    Document,
    DocumentRepository,
    get_document_repo,
)
from app.repositories.tag_repo import (
    DocumentTag,
    Tag,
    TagRepository,
    get_tag_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.document_service import (
    DocumentNotFoundError,
    DocumentService,
    DocumentValidationError,
    TagAlreadyExistsError,
    get_document_service,
)

# Ensure routers are registered
if not any(getattr(r, "path", "").startswith("/api/v1/documents") for r in app.routes):
    app.include_router(documents_router)
if not any(getattr(r, "path", "").startswith("/api/v1/tags") for r in app.routes):
    app.include_router(tags_router)


# ---------------------------------------------------------------------------
# In-Memory Test Doubles
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
    """In-memory CourseRepository double."""

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
        self, course_id: uuid.UUID, user_id: uuid.UUID
    ) -> Course | None:
        course = self.courses.get(course_id)
        if course and course.user_id == user_id:
            return course
        return None


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


class FakeTagRepository(TagRepository):
    """In-memory TagRepository double."""

    def __init__(self) -> None:
        self.tags: dict[uuid.UUID, Tag] = {}
        # List of (document_id, tag_id, user_id, created_at)
        self.document_tags: list[DocumentTag] = []

    async def create_tag(self, user_id: uuid.UUID, name: str) -> Tag:
        clean = name.strip()
        norm = clean.lower()
        tag = Tag(
            id=uuid.uuid4(),
            user_id=user_id,
            name=clean,
            normalized_name=norm,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.tags[tag.id] = tag
        return tag

    async def get_tag_by_id(self, user_id: uuid.UUID, tag_id: uuid.UUID) -> Tag | None:
        t = self.tags.get(tag_id)
        if t and t.user_id == user_id:
            return t
        return None

    async def get_tag_by_normalized_name(
        self, user_id: uuid.UUID, normalized_name: str
    ) -> Tag | None:
        norm = normalized_name.strip().lower()
        for t in self.tags.values():
            if t.user_id == user_id and t.normalized_name == norm:
                return t
        return None

    async def get_or_create_tag(self, user_id: uuid.UUID, name: str) -> Tag:
        existing = await self.get_tag_by_normalized_name(user_id, name)
        if existing:
            return existing
        return await self.create_tag(user_id, name)

    async def list_tags_by_user(self, user_id: uuid.UUID) -> list[tuple[Tag, int]]:
        user_tags = [t for t in self.tags.values() if t.user_id == user_id]
        user_tags.sort(key=lambda x: x.name.lower())
        results: list[tuple[Tag, int]] = []
        for t in user_tags:
            # Count distinct documents with this tag
            docs = {
                dt.document_id
                for dt in self.document_tags
                if dt.tag_id == t.id and dt.user_id == user_id
            }
            results.append((t, len(docs)))
        return results

    async def delete_tag(self, user_id: uuid.UUID, tag_id: uuid.UUID) -> bool:
        t = await self.get_tag_by_id(user_id, tag_id)
        if not t:
            return False
        del self.tags[tag_id]
        self.document_tags = [
            dt for dt in self.document_tags if not (dt.tag_id == tag_id and dt.user_id == user_id)
        ]
        return True

    async def tag_document(
        self, user_id: uuid.UUID, document_id: uuid.UUID, tag_id: uuid.UUID
    ) -> DocumentTag:
        for dt in self.document_tags:
            if (
                dt.document_id == document_id
                and dt.tag_id == tag_id
                and dt.user_id == user_id
            ):
                return dt
        dt = DocumentTag(
            document_id=document_id,
            tag_id=tag_id,
            user_id=user_id,
            created_at=datetime.now(UTC),
        )
        self.document_tags.append(dt)
        return dt

    async def untag_document(
        self, user_id: uuid.UUID, document_id: uuid.UUID, tag_id: uuid.UUID
    ) -> bool:
        before = len(self.document_tags)
        self.document_tags = [
            dt
            for dt in self.document_tags
            if not (
                dt.document_id == document_id
                and dt.tag_id == tag_id
                and dt.user_id == user_id
            )
        ]
        return len(self.document_tags) < before

    async def get_tags_for_document(
        self, user_id: uuid.UUID, document_id: uuid.UUID
    ) -> list[Tag]:
        matching_tag_ids = [
            dt.tag_id
            for dt in self.document_tags
            if dt.document_id == document_id and dt.user_id == user_id
        ]
        tags = [
            self.tags[tid]
            for tid in matching_tag_ids
            if tid in self.tags and self.tags[tid].user_id == user_id
        ]
        tags.sort(key=lambda x: x.name.lower())
        return tags

    async def get_tags_for_documents(
        self, user_id: uuid.UUID, document_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[Tag]]:
        mapping: dict[uuid.UUID, list[Tag]] = {doc_id: [] for doc_id in document_ids}
        for doc_id in document_ids:
            mapping[doc_id] = await self.get_tags_for_document(user_id, doc_id)
        return mapping


class FakeDocumentRepository(DocumentRepository):
    """In-memory DocumentRepository double with search, filtering, and tag support."""

    def __init__(self, tag_repo: FakeTagRepository | None = None) -> None:
        self.documents: dict[uuid.UUID, Document] = {}
        self.tag_repo = tag_repo

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
                d
                for d in matching
                if sq in d.title.lower() or sq in d.original_file_name.lower()
            ]

        if file_type and file_type.strip():
            ft = file_type.strip().lower().lstrip(".")
            ext_map = {
                "pdf": "application/pdf",
                "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
            }
            if ft in ext_map:
                target_mime = ext_map[ft]
                matching = [d for d in matching if d.file_type == target_mime]
            else:
                matching = [d for d in matching if ft in d.file_type.lower()]

        if tag and tag.strip() and self.tag_repo:
            clean_tag = tag.strip().lower()
            matching_doc_ids: set[uuid.UUID] = set()
            for doc in matching:
                doc_tags = await self.tag_repo.get_tags_for_document(user_id, doc.id)
                if any(
                    t.normalized_name == clean_tag or t.name.lower() == clean_tag
                    for t in doc_tags
                ):
                    matching_doc_ids.add(doc.id)
            matching = [d for d in matching if d.id in matching_doc_ids]

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
def fake_storage() -> FakeStorageClient:
    return FakeStorageClient()


@pytest.fixture
def fake_tag_repo() -> FakeTagRepository:
    return FakeTagRepository()


@pytest.fixture
def fake_doc_repo(fake_tag_repo: FakeTagRepository) -> FakeDocumentRepository:
    return FakeDocumentRepository(tag_repo=fake_tag_repo)


@pytest.fixture
def document_service(
    fake_doc_repo: FakeDocumentRepository,
    fake_course_repo: FakeCourseRepository,
    fake_storage: FakeStorageClient,
    fake_tag_repo: FakeTagRepository,
) -> DocumentService:
    return DocumentService(
        document_repo=fake_doc_repo,
        course_repo=fake_course_repo,
        storage_client=fake_storage,
        tag_repo=fake_tag_repo,
    )


@pytest_asyncio.fixture
async def authenticated_student(
    fake_user_repo: FakeUserRepository,
) -> tuple[User, str]:
    pw_hash = hash_password("SecretPass123!")
    user = await fake_user_repo.create_user(
        email="search_student@university.edu",
        password_hash=pw_hash,
        full_name="Search Tester",
        role_name="student",
    )
    token = create_access_token(
        subject=str(user.id),
        expires_delta=timedelta(hours=1),
    )
    return user, token


@pytest_asyncio.fixture
async def other_student(
    fake_user_repo: FakeUserRepository,
) -> tuple[User, str]:
    pw_hash = hash_password("SecretPass456!")
    user = await fake_user_repo.create_user(
        email="other_student@university.edu",
        password_hash=pw_hash,
        full_name="Other Student",
        role_name="student",
    )
    token = create_access_token(
        subject=str(user.id),
        expires_delta=timedelta(hours=1),
    )
    return user, token


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_doc_repo: FakeDocumentRepository,
    fake_course_repo: FakeCourseRepository,
    fake_storage: FakeStorageClient,
    fake_tag_repo: FakeTagRepository,
    document_service: DocumentService,
) -> AsyncGenerator[AsyncClient, None]:
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_document_repo] = lambda: fake_doc_repo
    app.dependency_overrides[get_course_repo] = lambda: fake_course_repo
    app.dependency_overrides[get_storage_client] = lambda: fake_storage
    app.dependency_overrides[get_tag_repo] = lambda: fake_tag_repo
    app.dependency_overrides[get_document_service] = lambda: document_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Unit Tests: Tag Repository
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tag_repo_create_and_normalization(fake_tag_repo: FakeTagRepository) -> None:
    """Verifies Tag creation automatically normalizes name by stripping and lowercasing."""
    user_id = uuid.uuid4()
    tag = await fake_tag_repo.create_tag(user_id=user_id, name="  Calculus II  ")
    assert tag.name == "Calculus II"
    assert tag.normalized_name == "calculus ii"
    assert tag.user_id == user_id

    # get_or_create returns existing tag when queried with different casing
    same_tag = await fake_tag_repo.get_or_create_tag(user_id=user_id, name="calculus ii")
    assert same_tag.id == tag.id


@pytest.mark.asyncio
async def test_tag_repo_tag_untag_and_batch_retrieval(fake_tag_repo: FakeTagRepository) -> None:
    """Verifies attaching tags to documents, untagging, and batch tag retrieval."""
    user_id = uuid.uuid4()
    doc1_id = uuid.uuid4()
    doc2_id = uuid.uuid4()

    t1 = await fake_tag_repo.create_tag(user_id, "Algebra")
    t2 = await fake_tag_repo.create_tag(user_id, "Geometry")

    # Tag doc1 with Algebra and Geometry, doc2 with Algebra
    await fake_tag_repo.tag_document(user_id, doc1_id, t1.id)
    await fake_tag_repo.tag_document(user_id, doc1_id, t2.id)
    await fake_tag_repo.tag_document(user_id, doc2_id, t1.id)

    # Document 1 has 2 tags
    doc1_tags = await fake_tag_repo.get_tags_for_document(user_id, doc1_id)
    assert [t.name for t in doc1_tags] == ["Algebra", "Geometry"]

    # Batch retrieval
    batch = await fake_tag_repo.get_tags_for_documents(user_id, [doc1_id, doc2_id])
    assert len(batch[doc1_id]) == 2
    assert len(batch[doc2_id]) == 1

    # Untag Geometry from doc1
    removed = await fake_tag_repo.untag_document(user_id, doc1_id, t2.id)
    assert removed is True
    doc1_tags_after = await fake_tag_repo.get_tags_for_document(user_id, doc1_id)
    assert [t.name for t in doc1_tags_after] == ["Algebra"]


@pytest.mark.asyncio
async def test_tag_repo_list_tags_with_document_counts(fake_tag_repo: FakeTagRepository) -> None:
    """Verifies list_tags_by_user accurately aggregates document counts."""
    user_id = uuid.uuid4()
    d1 = uuid.uuid4()
    d2 = uuid.uuid4()

    t_ai = await fake_tag_repo.create_tag(user_id, "Artificial Intelligence")
    t_db = await fake_tag_repo.create_tag(user_id, "Database Systems")

    await fake_tag_repo.tag_document(user_id, d1, t_ai.id)
    await fake_tag_repo.tag_document(user_id, d2, t_ai.id)
    await fake_tag_repo.tag_document(user_id, d1, t_db.id)

    tag_list = await fake_tag_repo.list_tags_by_user(user_id)
    count_map = {t.name: c for t, c in tag_list}
    assert count_map["Artificial Intelligence"] == 2
    assert count_map["Database Systems"] == 1


@pytest.mark.asyncio
async def test_tag_repo_tenant_isolation(fake_tag_repo: FakeTagRepository) -> None:
    """Verifies User A cannot view, retrieve, delete, or tag with User B's tags."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    tag_a = await fake_tag_repo.create_tag(user_a, "User A Secret")

    # User B cannot get tag A
    assert await fake_tag_repo.get_tag_by_id(user_b, tag_a.id) is None
    # User B cannot delete tag A
    assert await fake_tag_repo.delete_tag(user_b, tag_a.id) is False
    # User B list does not show tag A
    tags_b = await fake_tag_repo.list_tags_by_user(user_b)
    assert len(tags_b) == 0


# ---------------------------------------------------------------------------
# Unit Tests: Document Service (Tags & Validation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_tag_validation_and_duplicate(
    document_service: DocumentService,
) -> None:
    """Verifies validation rules for tag names and duplicate rejection."""
    user_id = uuid.uuid4()

    # Empty name fails
    with pytest.raises(DocumentValidationError, match="cannot be empty"):
        await document_service.create_tag(user_id, "   ")

    # Name > 100 characters fails
    with pytest.raises(DocumentValidationError, match="cannot exceed 100"):
        await document_service.create_tag(user_id, "a" * 101)

    # First creation succeeds
    tag_dto = await document_service.create_tag(user_id, "Machine Learning")
    assert tag_dto.name == "Machine Learning"

    # Duplicate creation with same normalized name raises TagAlreadyExistsError
    with pytest.raises(TagAlreadyExistsError, match="already exists"):
        await document_service.create_tag(user_id, "machine learning")


@pytest.mark.asyncio
async def test_service_document_tag_foreign_ownership_guard(
    document_service: DocumentService,
    fake_doc_repo: FakeDocumentRepository,
) -> None:
    """Verifies that tagging another user's document raises DocumentNotFoundError."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    doc_a = await fake_doc_repo.create(
        user_id=user_a,
        title="User A Paper",
        original_file_name="paper.pdf",
        storage_object_key="user_a/paper.pdf",
        file_type="application/pdf",
        file_size_bytes=1000,
    )

    # User B attempts to tag User A's document
    with pytest.raises(DocumentNotFoundError):
        await document_service.add_tag_to_document(
            user_id=user_b,
            document_id=doc_a.id,
            tag_name="Unauthorized Tag",
        )

    # User B attempts to get tags for User A's document
    with pytest.raises(DocumentNotFoundError):
        await document_service.get_document_tags(
            user_id=user_b,
            document_id=doc_a.id,
        )


# ---------------------------------------------------------------------------
# API Integration Tests: Search, Filtering & Sorting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_search_query_matching(
    client: AsyncClient,
    authenticated_student: tuple[User, str],
    fake_doc_repo: FakeDocumentRepository,
) -> None:
    """Verifies GET /api/v1/documents?q= matches title or original_file_name."""
    user, token = authenticated_student
    headers = {"Authorization": f"Bearer {token}"}

    # Seed documents
    await fake_doc_repo.create(
        user_id=user.id,
        title="Introduction to Quantum Mechanics",
        original_file_name="physics_lec1.pdf",
        storage_object_key="qm.pdf",
        file_type="application/pdf",
        file_size_bytes=1000,
    )
    await fake_doc_repo.create(
        user_id=user.id,
        title="Advanced Linear Algebra Notes",
        original_file_name="quantum_math_cheatsheet.docx",
        storage_object_key="la.docx",
        file_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size_bytes=2000,
    )
    await fake_doc_repo.create(
        user_id=user.id,
        title="Biology 101 Ecology Chapter",
        original_file_name="ecology_slides.pdf",
        storage_object_key="bio.pdf",
        file_type="application/pdf",
        file_size_bytes=3000,
    )

    # Search for "quantum" (matches title of doc 1 and filename of doc 2)
    resp = await client.get("/api/v1/documents?q=quantum", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 2
    titles = [item["title"] for item in data["items"]]
    assert "Introduction to Quantum Mechanics" in titles
    assert "Advanced Linear Algebra Notes" in titles

    # Search for "Ecology"
    resp_eco = await client.get("/api/v1/documents?q=Ecology", headers=headers)
    assert resp_eco.status_code == 200
    assert resp_eco.json()["data"]["total"] == 1
    assert resp_eco.json()["data"]["items"][0]["title"] == "Biology 101 Ecology Chapter"

    # Search for nonexistent term
    resp_none = await client.get("/api/v1/documents?q=astrophysics", headers=headers)
    assert resp_none.status_code == 200
    assert resp_none.json()["data"]["total"] == 0
    assert len(resp_none.json()["data"]["items"]) == 0


@pytest.mark.asyncio
async def test_api_filter_by_file_type_and_tag(
    client: AsyncClient,
    authenticated_student: tuple[User, str],
    fake_doc_repo: FakeDocumentRepository,
    fake_tag_repo: FakeTagRepository,
) -> None:
    """Verifies filtering by file_type and attached tag name."""
    user, token = authenticated_student
    headers = {"Authorization": f"Bearer {token}"}

    doc_pdf = await fake_doc_repo.create(
        user_id=user.id,
        title="Midterm Review Guide",
        original_file_name="midterm.pdf",
        storage_object_key="midterm.pdf",
        file_type="application/pdf",
        file_size_bytes=5000,
    )
    await fake_doc_repo.create(
        user_id=user.id,
        title="Assignment 1 Solutions",
        original_file_name="assignment1.docx",
        storage_object_key="assign1.docx",
        file_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size_bytes=2500,
    )

    # Attach tag 'exam-prep' to doc_pdf
    tag_exam = await fake_tag_repo.create_tag(user.id, "Exam Prep")
    await fake_tag_repo.tag_document(user.id, doc_pdf.id, tag_exam.id)

    # 1. Filter by file_type="application/pdf"
    resp_pdf = await client.get("/api/v1/documents?file_type=application/pdf", headers=headers)
    assert resp_pdf.status_code == 200
    assert resp_pdf.json()["data"]["total"] == 1
    assert resp_pdf.json()["data"]["items"][0]["title"] == "Midterm Review Guide"

    # 2. Filter by file_type substring "docx"
    resp_docx = await client.get("/api/v1/documents?file_type=docx", headers=headers)
    assert resp_docx.status_code == 200
    assert resp_docx.json()["data"]["total"] == 1
    assert resp_docx.json()["data"]["items"][0]["title"] == "Assignment 1 Solutions"

    # 3. Filter by tag="Exam Prep" (also case-insensitive "exam prep")
    resp_tag = await client.get("/api/v1/documents?tag=exam prep", headers=headers)
    assert resp_tag.status_code == 200
    assert resp_tag.json()["data"]["total"] == 1
    assert resp_tag.json()["data"]["items"][0]["title"] == "Midterm Review Guide"
    assert resp_tag.json()["data"]["items"][0]["tags"] == ["Exam Prep"]


@pytest.mark.asyncio
async def test_api_sorting_options(
    client: AsyncClient,
    authenticated_student: tuple[User, str],
    fake_doc_repo: FakeDocumentRepository,
) -> None:
    """Verifies sort_by and sort_order options (title, file_size_bytes, created_at)."""
    user, token = authenticated_student
    headers = {"Authorization": f"Bearer {token}"}

    base_time = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)

    d1 = await fake_doc_repo.create(
        user_id=user.id,
        title="Alpha Document",
        original_file_name="alpha.pdf",
        storage_object_key="alpha.pdf",
        file_type="application/pdf",
        file_size_bytes=5000,
    )
    d1.created_at = base_time + timedelta(hours=1)

    d2 = await fake_doc_repo.create(
        user_id=user.id,
        title="Beta Document",
        original_file_name="beta.pdf",
        storage_object_key="beta.pdf",
        file_type="application/pdf",
        file_size_bytes=10000,
    )
    d2.created_at = base_time + timedelta(hours=2)

    d3 = await fake_doc_repo.create(
        user_id=user.id,
        title="Gamma Document",
        original_file_name="gamma.pdf",
        storage_object_key="gamma.pdf",
        file_type="application/pdf",
        file_size_bytes=2000,
    )
    d3.created_at = base_time + timedelta(hours=3)

    # Sort by title ASC
    resp_title_asc = await client.get(
        "/api/v1/documents?sort_by=title&sort_order=asc", headers=headers
    )
    assert resp_title_asc.status_code == 200
    titles = [i["title"] for i in resp_title_asc.json()["data"]["items"]]
    assert titles == ["Alpha Document", "Beta Document", "Gamma Document"]

    # Sort by title DESC
    resp_title_desc = await client.get(
        "/api/v1/documents?sort_by=title&sort_order=desc", headers=headers
    )
    assert resp_title_desc.status_code == 200
    titles_desc = [i["title"] for i in resp_title_desc.json()["data"]["items"]]
    assert titles_desc == ["Gamma Document", "Beta Document", "Alpha Document"]

    # Sort by file_size_bytes ASC
    resp_size_asc = await client.get(
        "/api/v1/documents?sort_by=file_size_bytes&sort_order=asc", headers=headers
    )
    assert resp_size_asc.status_code == 200
    sizes = [i["file_size_bytes"] for i in resp_size_asc.json()["data"]["items"]]
    assert sizes == [2000, 5000, 10000]


@pytest.mark.asyncio
async def test_api_pagination(
    client: AsyncClient,
    authenticated_student: tuple[User, str],
    fake_doc_repo: FakeDocumentRepository,
) -> None:
    """Verifies limit and skip pagination on documents endpoint."""
    user, token = authenticated_student
    headers = {"Authorization": f"Bearer {token}"}

    for i in range(5):
        await fake_doc_repo.create(
            user_id=user.id,
            title=f"Doc {i}",
            original_file_name=f"doc_{i}.pdf",
            storage_object_key=f"doc_{i}.pdf",
            file_type="application/pdf",
            file_size_bytes=1000 + i,
        )

    # Page 1: limit 2, skip 0
    p1 = await client.get("/api/v1/documents?limit=2&skip=0", headers=headers)
    assert p1.status_code == 200
    d1 = p1.json()["data"]
    assert d1["total"] == 5
    assert len(d1["items"]) == 2
    assert d1["skip"] == 0

    # Page 2: limit 2, skip 2
    p2 = await client.get("/api/v1/documents?limit=2&skip=2", headers=headers)
    assert p2.status_code == 200
    d2 = p2.json()["data"]
    assert d2["total"] == 5
    assert len(d2["items"]) == 2
    assert d2["skip"] == 2

    # Page 3: limit 2, skip 4
    p3 = await client.get("/api/v1/documents?limit=2&skip=4", headers=headers)
    assert p3.status_code == 200
    d3 = p3.json()["data"]
    assert d3["total"] == 5
    assert len(d3["items"]) == 1


# ---------------------------------------------------------------------------
# API Integration Tests: Tag Endpoints (/api/v1/tags)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_tags_crud_flow(
    client: AsyncClient,
    authenticated_student: tuple[User, str],
) -> None:
    """Verifies POST /api/v1/tags, GET /api/v1/tags, and DELETE /api/v1/tags/{tag_id}."""
    _, token = authenticated_student
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Create tag
    resp_create = await client.post(
        "/api/v1/tags",
        json={"name": "Computer Architecture"},
        headers=headers,
    )
    assert resp_create.status_code == 201
    created_data = resp_create.json()["data"]
    tag_id = created_data["id"]
    assert created_data["name"] == "Computer Architecture"
    assert created_data["document_count"] == 0

    # 2. Duplicate normalized creation fails with 409
    resp_dup = await client.post(
        "/api/v1/tags",
        json={"name": "  computer architecture  "},
        headers=headers,
    )
    assert resp_dup.status_code == 409

    # 3. List tags
    resp_list = await client.get("/api/v1/tags", headers=headers)
    assert resp_list.status_code == 200
    list_data = resp_list.json()["data"]
    assert list_data["total"] == 1
    assert list_data["items"][0]["id"] == tag_id

    # 4. Delete tag
    resp_del = await client.delete(f"/api/v1/tags/{tag_id}", headers=headers)
    assert resp_del.status_code == 200
    assert resp_del.json()["success"] is True

    # 5. Delete again returns 404
    resp_del_again = await client.delete(f"/api/v1/tags/{tag_id}", headers=headers)
    assert resp_del_again.status_code == 404

    # 6. List is now empty
    resp_list_after = await client.get("/api/v1/tags", headers=headers)
    assert resp_list_after.json()["data"]["total"] == 0


# ---------------------------------------------------------------------------
# API Integration Tests: Document Tags Sub-endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_document_tags_subendpoints_lifecycle(
    client: AsyncClient,
    authenticated_student: tuple[User, str],
    fake_doc_repo: FakeDocumentRepository,
) -> None:
    """Verifies adding tag to document, listing tags on document, and removing tag."""
    user, token = authenticated_student
    headers = {"Authorization": f"Bearer {token}"}

    doc = await fake_doc_repo.create(
        user_id=user.id,
        title="Operating Systems Concept",
        original_file_name="os.pdf",
        storage_object_key="os.pdf",
        file_type="application/pdf",
        file_size_bytes=4000,
    )

    # 1. Attach tag "Kernel"
    resp_tag = await client.post(
        f"/api/v1/documents/{doc.id}/tags",
        json={"name": "Kernel"},
        headers=headers,
    )
    assert resp_tag.status_code == 201
    tag_data = resp_tag.json()["data"]
    tag_id = tag_data["id"]
    assert tag_data["name"] == "Kernel"

    # 2. Get tags for this document
    resp_get_tags = await client.get(
        f"/api/v1/documents/{doc.id}/tags",
        headers=headers,
    )
    assert resp_get_tags.status_code == 200
    assert resp_get_tags.json()["data"]["total"] == 1
    assert resp_get_tags.json()["data"]["items"][0]["name"] == "Kernel"

    # 3. GET /api/v1/documents includes tag in list
    resp_docs = await client.get("/api/v1/documents", headers=headers)
    assert resp_docs.status_code == 200
    items = resp_docs.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["tags"] == ["Kernel"]

    # 4. Remove tag from document
    resp_untag = await client.delete(
        f"/api/v1/documents/{doc.id}/tags/{tag_id}",
        headers=headers,
    )
    assert resp_untag.status_code == 200

    # 5. Document tags list is now empty
    resp_tags_after = await client.get(
        f"/api/v1/documents/{doc.id}/tags",
        headers=headers,
    )
    assert resp_tags_after.json()["data"]["total"] == 0


# ---------------------------------------------------------------------------
# API Integration Tests: Strict Tenant Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_strict_tenant_isolation_complete(
    client: AsyncClient,
    authenticated_student: tuple[User, str],
    other_student: tuple[User, str],
    fake_doc_repo: FakeDocumentRepository,
    fake_tag_repo: FakeTagRepository,
) -> None:
    """Verifies User A cannot search, view, attach tags to, or delete User B's resources."""
    _, token_a = authenticated_student
    user_b, _ = other_student

    headers_a = {"Authorization": f"Bearer {token_a}"}

    # User B creates document and tag
    doc_b = await fake_doc_repo.create(
        user_id=user_b.id,
        title="Student B Confidential Research",
        original_file_name="confidential.pdf",
        storage_object_key="b/confidential.pdf",
        file_type="application/pdf",
        file_size_bytes=8000,
    )
    tag_b = await fake_tag_repo.create_tag(user_b.id, "B-Secret-Tag")
    await fake_tag_repo.tag_document(user_b.id, doc_b.id, tag_b.id)

    # 1. User A searches with keyword "Confidential" -> receives 0 results
    resp_search = await client.get("/api/v1/documents?q=Confidential", headers=headers_a)
    assert resp_search.status_code == 200
    assert resp_search.json()["data"]["total"] == 0

    # 2. User A filters with tag="B-Secret-Tag" -> receives 0 results
    resp_tag_search = await client.get("/api/v1/documents?tag=B-Secret-Tag", headers=headers_a)
    assert resp_tag_search.status_code == 200
    assert resp_tag_search.json()["data"]["total"] == 0

    # 3. User A attempts to list tags -> does not see User B's tag
    resp_tags_list = await client.get("/api/v1/tags", headers=headers_a)
    assert resp_tags_list.status_code == 200
    assert resp_tags_list.json()["data"]["total"] == 0

    # 4. User A attempts to delete User B's tag -> 404 Not Found
    resp_del_tag = await client.delete(f"/api/v1/tags/{tag_b.id}", headers=headers_a)
    assert resp_del_tag.status_code == 404

    # 5. User A attempts to add a tag to User B's document -> 404 Not Found
    resp_tag_doc = await client.post(
        f"/api/v1/documents/{doc_b.id}/tags",
        json={"name": "AttackerTag"},
        headers=headers_a,
    )
    assert resp_tag_doc.status_code == 404

    # 6. User A attempts to get tags of User B's document -> 404 Not Found
    resp_get_doc_tags = await client.get(
        f"/api/v1/documents/{doc_b.id}/tags",
        headers=headers_a,
    )
    assert resp_get_doc_tags.status_code == 404


# ---------------------------------------------------------------------------
# SQL Repository Tests (Statement Compilation & Execution with Mock Session)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sql_tag_repository_statements() -> None:
    """Verifies that SQL TagRepository properly formats SQLAlchemy statements."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    repo = TagRepository(mock_session)

    user_id = uuid.uuid4()
    tag_id = uuid.uuid4()
    doc_id = uuid.uuid4()

    # 1. create_tag
    tag = await repo.create_tag(user_id=user_id, name="  Bioinformatics  ")
    assert tag.name == "Bioinformatics"
    assert tag.normalized_name == "bioinformatics"
    assert mock_session.add.called
    assert mock_session.flush.called

    # 2. get_tag_by_id executes select
    mock_session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=tag))
    fetched = await repo.get_tag_by_id(user_id=user_id, tag_id=tag_id)
    assert fetched == tag

    # 3. list_tags_by_user executes group by query
    mock_session.execute.return_value = MagicMock(all=MagicMock(return_value=[(tag, 3)]))
    items = await repo.list_tags_by_user(user_id=user_id)
    assert items == [(tag, 3)]

    # 4. tag_document creates DocumentTag
    mock_session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    doc_tag = await repo.tag_document(user_id=user_id, document_id=doc_id, tag_id=tag_id)
    assert doc_tag.document_id == doc_id
    assert doc_tag.tag_id == tag_id
    assert doc_tag.user_id == user_id


@pytest.mark.asyncio
async def test_sql_document_repository_search_filtering_statements() -> None:
    """Verifies that DocumentRepository.list_by_user builds parameterized query with all filters."""
    mock_session = AsyncMock()
    repo = DocumentRepository(mock_session)

    user_id = uuid.uuid4()
    course_id = uuid.uuid4()

    mock_count_result = MagicMock()
    mock_count_result.scalar.return_value = 1
    mock_items_result = MagicMock()
    mock_items_result.scalars.return_value.all.return_value = []

    mock_session.execute.side_effect = [mock_count_result, mock_items_result]

    docs, total = await repo.list_by_user(
        user_id=user_id,
        course_id=course_id,
        status="embedded",
        search_query="algorithms",
        file_type="application/pdf",
        tag="exam",
        sort_by="title",
        sort_order="asc",
        skip=0,
        limit=20,
    )
    assert total == 1
    assert docs == []
    assert mock_session.execute.call_count == 2
