"""Comprehensive tests for courses CRUD repository, service, and router (US 2.5)."""

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure backend root is on sys.path for direct execution
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.courses import router as courses_router
from app.core.security import create_access_token, hash_password
from app.main import app
from app.repositories.course_repo import (
    Course,
    CourseRepository,
    get_course_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.course_service import (
    CourseConflictError,
    CourseNotFoundError,
    CourseService,
    CourseValidationError,
    get_course_service,
)

# Ensure courses router is mounted on app
if not any(getattr(route, "path", "").startswith("/api/v1/courses") for route in app.routes):
    app.include_router(courses_router)


# ---------------------------------------------------------------------------
# Test Doubles: In-Memory Fake Repositories
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for authentication in courses testing."""

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


class FakeCourseRepository(CourseRepository):
    """In-memory CourseRepository double for isolated testing."""

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
        course = Course(
            id=uuid.uuid4(),
            user_id=user_id,
            name=name.strip(),
            course_code=course_code.strip() if course_code else None,
            description=description.strip() if description else None,
            instructor=instructor.strip() if instructor else None,
            semester=semester.strip() if semester else None,
            academic_year=academic_year.strip() if academic_year else None,
            status=status,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.courses[course.id] = course
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

    async def get_by_code_semester_year(
        self,
        user_id: uuid.UUID,
        course_code: str | None,
        semester: str | None,
        academic_year: str | None,
    ) -> Course | None:
        if not course_code:
            return None
        norm_code = course_code.strip().lower()
        norm_sem = semester.strip() if semester else None
        norm_year = academic_year.strip() if academic_year else None
        for c in self.courses.values():
            if c.user_id == user_id:
                c_code = c.course_code.strip().lower() if c.course_code else None
                c_sem = c.semester.strip() if c.semester else None
                c_year = c.academic_year.strip() if c.academic_year else None
                if c_code == norm_code and c_sem == norm_sem and c_year == norm_year:
                    return c
        return None

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        status: str | None = None,
        semester: str | None = None,
        academic_year: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Course], int]:
        matching = []
        for c in self.courses.values():
            if c.user_id != user_id:
                continue
            if status and c.status != status:
                continue
            if semester and c.semester != semester.strip():
                continue
            if academic_year and c.academic_year != academic_year.strip():
                continue
            matching.append(c)

        # Sort newest first
        matching.sort(key=lambda x: x.created_at, reverse=True)
        total = len(matching)
        paginated = matching[skip : skip + limit]
        return paginated, total

    async def update(self, course: Course, **kwargs: Any) -> Course:
        allowed_fields = {
            "name",
            "course_code",
            "description",
            "instructor",
            "semester",
            "academic_year",
            "status",
        }
        for key, value in kwargs.items():
            if key in allowed_fields:
                setattr(course, key, value)
        course.updated_at = datetime.now(UTC)
        return course

    async def delete(self, course: Course) -> None:
        self.courses.pop(course.id, None)


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
def course_service(fake_course_repo: FakeCourseRepository) -> CourseService:
    return CourseService(course_repo=fake_course_repo)


@pytest_asyncio.fixture
async def student_a(fake_user_repo: FakeUserRepository) -> User:
    """Pre-create primary active student."""
    return await fake_user_repo.create_user(
        email="student.a@university.edu",
        password_hash=hash_password("ValidPass123!"),
        full_name="Alice Student",
    )


@pytest_asyncio.fixture
async def student_b(fake_user_repo: FakeUserRepository) -> User:
    """Pre-create secondary active student for tenant isolation checks."""
    return await fake_user_repo.create_user(
        email="student.b@university.edu",
        password_hash=hash_password("ValidPass123!"),
        full_name="Bob Student",
    )


@pytest_asyncio.fixture
async def inactive_student(fake_user_repo: FakeUserRepository) -> User:
    """Pre-create a deactivated student."""
    user = await fake_user_repo.create_user(
        email="inactive.student@university.edu",
        password_hash=hash_password("ValidPass123!"),
        full_name="Inactive Student",
    )
    user.is_active = False
    return user


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_course_repo: FakeCourseRepository,
) -> AsyncGenerator[AsyncClient, None]:
    """Test HTTP client with dependency overrides for repositories and services."""
    service = CourseService(course_repo=fake_course_repo)
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_course_repo] = lambda: fake_course_repo
    app.dependency_overrides[get_course_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_headers(user_id: uuid.UUID, expires_seconds: int = 3600) -> dict[str, str]:
    """Helper to generate Authorization header with access token."""
    token = create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(seconds=expires_seconds),
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Repository Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_create_course(fake_course_repo: FakeCourseRepository) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(
        user_id=user_id,
        name="Data Structures & Algorithms",
        course_code="CS201",
        semester="Fall 2026",
        academic_year="2026-2027",
    )
    assert course.id is not None
    assert course.user_id == user_id
    assert course.name == "Data Structures & Algorithms"
    assert course.course_code == "CS201"
    assert course.status == "active"


@pytest.mark.asyncio
async def test_repo_get_by_id_and_user_found(fake_course_repo: FakeCourseRepository) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Operating Systems")
    found = await fake_course_repo.get_by_id_and_user(course.id, user_id)
    assert found is not None
    assert found.id == course.id


@pytest.mark.asyncio
async def test_repo_get_by_id_and_user_isolation(fake_course_repo: FakeCourseRepository) -> None:
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_a, name="Computer Networks")
    found = await fake_course_repo.get_by_id_and_user(course.id, user_b)
    assert found is None


@pytest.mark.asyncio
async def test_repo_list_by_user_pagination(fake_course_repo: FakeCourseRepository) -> None:
    user_id = uuid.uuid4()
    for i in range(5):
        await fake_course_repo.create(user_id=user_id, name=f"Course {i}")

    items, total = await fake_course_repo.list_by_user(user_id=user_id, skip=1, limit=2)
    assert total == 5
    assert len(items) == 2


@pytest.mark.asyncio
async def test_repo_update_and_delete(fake_course_repo: FakeCourseRepository) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Calculus I")
    updated = await fake_course_repo.update(course, name="Calculus II", status="archived")
    assert updated.name == "Calculus II"
    assert updated.status == "archived"

    await fake_course_repo.delete(course)
    assert await fake_course_repo.get_by_id_and_user(course.id, user_id) is None


# ---------------------------------------------------------------------------
# Service Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_create_course_success(course_service: CourseService) -> None:
    user_id = uuid.uuid4()
    dto = await course_service.create_course(
        user_id=user_id,
        name="Linear Algebra",
        course_code="MATH102",
        semester="Spring 2026",
        academic_year="2025-2026",
    )
    assert dto.name == "Linear Algebra"
    assert dto.course_code == "MATH102"
    assert dto.status == "active"


@pytest.mark.asyncio
async def test_service_create_course_duplicate_conflict(course_service: CourseService) -> None:
    user_id = uuid.uuid4()
    await course_service.create_course(
        user_id=user_id,
        name="Linear Algebra",
        course_code="MATH102",
        semester="Spring 2026",
        academic_year="2025-2026",
    )
    with pytest.raises(CourseConflictError):
        await course_service.create_course(
            user_id=user_id,
            name="Advanced Linear Algebra",
            course_code="MATH102",
            semester="Spring 2026",
            academic_year="2025-2026",
        )


@pytest.mark.asyncio
async def test_service_create_course_validation_error(course_service: CourseService) -> None:
    user_id = uuid.uuid4()
    with pytest.raises(CourseValidationError):
        await course_service.create_course(user_id=user_id, name="   ")

    with pytest.raises(CourseValidationError):
        await course_service.create_course(user_id=user_id, name="Valid Name", status="invalid_status")


@pytest.mark.asyncio
async def test_service_get_course_not_found(course_service: CourseService) -> None:
    user_id = uuid.uuid4()
    with pytest.raises(CourseNotFoundError):
        await course_service.get_course(uuid.uuid4(), user_id)


@pytest.mark.asyncio
async def test_service_update_course_conflict(course_service: CourseService) -> None:
    user_id = uuid.uuid4()
    await course_service.create_course(
        user_id=user_id,
        name="Course 1",
        course_code="CS101",
        semester="Fall 2026",
        academic_year="2026",
    )
    c2 = await course_service.create_course(
        user_id=user_id,
        name="Course 2",
        course_code="CS102",
        semester="Fall 2026",
        academic_year="2026",
    )
    with pytest.raises(CourseConflictError):
        await course_service.update_course(
            course_id=c2.id,
            user_id=user_id,
            course_code="CS101",
        )


@pytest.mark.asyncio
async def test_service_archive_and_delete_course(course_service: CourseService) -> None:
    user_id = uuid.uuid4()
    c = await course_service.create_course(user_id=user_id, name="Physics")
    archived = await course_service.archive_course(c.id, user_id)
    assert archived.status == "archived"

    await course_service.delete_course(c.id, user_id)
    with pytest.raises(CourseNotFoundError):
        await course_service.get_course(c.id, user_id)


# ---------------------------------------------------------------------------
# API Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_create_course_201(client: AsyncClient, student_a: User) -> None:
    headers = make_auth_headers(student_a.id)
    payload = {
        "name": "Database Systems",
        "course_code": "CS301",
        "description": "Relational algebra and SQL indexing",
        "instructor": "Dr. Smith",
        "semester": "Fall 2026",
        "academic_year": "2026-2027",
        "status": "active",
    }
    resp = await client.post("/api/v1/courses", json=payload, headers=headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["name"] == "Database Systems"
    assert body["data"]["course_code"] == "CS301"
    assert body["data"]["user_id"] == str(student_a.id)


@pytest.mark.asyncio
async def test_api_create_course_duplicate_409(client: AsyncClient, student_a: User) -> None:
    headers = make_auth_headers(student_a.id)
    payload = {
        "name": "Software Engineering",
        "course_code": "SE101",
        "semester": "Spring 2026",
        "academic_year": "2025-2026",
    }
    resp1 = await client.post("/api/v1/courses", json=payload, headers=headers)
    assert resp1.status_code == 201

    resp2 = await client.post("/api/v1/courses", json=payload, headers=headers)
    assert resp2.status_code == 409
    body = resp2.json()
    assert body["success"] is False
    assert "already exists" in body["error"]["message"]


@pytest.mark.asyncio
async def test_api_create_course_invalid_name_422(client: AsyncClient, student_a: User) -> None:
    headers = make_auth_headers(student_a.id)
    payload = {"name": "   "}
    resp = await client.post("/api/v1/courses", json=payload, headers=headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_list_courses_tenant_isolation(
    client: AsyncClient,
    student_a: User,
    student_b: User,
) -> None:
    headers_a = make_auth_headers(student_a.id)
    headers_b = make_auth_headers(student_b.id)

    # Student A creates 2 courses
    await client.post("/api/v1/courses", json={"name": "A Course 1"}, headers=headers_a)
    await client.post("/api/v1/courses", json={"name": "A Course 2"}, headers=headers_a)

    # Student B creates 1 course
    await client.post("/api/v1/courses", json={"name": "B Course 1"}, headers=headers_b)

    # Student A lists courses
    resp_a = await client.get("/api/v1/courses", headers=headers_a)
    assert resp_a.status_code == 200
    data_a = resp_a.json()["data"]
    assert data_a["total"] == 2
    assert all(item["user_id"] == str(student_a.id) for item in data_a["items"])

    # Student B lists courses
    resp_b = await client.get("/api/v1/courses", headers=headers_b)
    assert resp_b.status_code == 200
    data_b = resp_b.json()["data"]
    assert data_b["total"] == 1
    assert data_b["items"][0]["name"] == "B Course 1"
    assert data_b["items"][0]["user_id"] == str(student_b.id)


@pytest.mark.asyncio
async def test_api_list_courses_filters(client: AsyncClient, student_a: User) -> None:
    headers = make_auth_headers(student_a.id)

    await client.post(
        "/api/v1/courses",
        json={"name": "Active Course", "status": "active", "semester": "Fall 2026"},
        headers=headers,
    )
    await client.post(
        "/api/v1/courses",
        json={"name": "Archived Course", "status": "archived", "semester": "Spring 2026"},
        headers=headers,
    )

    # Filter active
    resp_active = await client.get("/api/v1/courses?status=active", headers=headers)
    assert resp_active.status_code == 200
    items_active = resp_active.json()["data"]["items"]
    assert len(items_active) == 1
    assert items_active[0]["name"] == "Active Course"

    # Filter archived
    resp_archived = await client.get("/api/v1/courses?status=archived", headers=headers)
    assert resp_archived.status_code == 200
    items_archived = resp_archived.json()["data"]["items"]
    assert len(items_archived) == 1
    assert items_archived[0]["name"] == "Archived Course"

    # Filter semester
    resp_sem = await client.get("/api/v1/courses?semester=Fall%202026", headers=headers)
    assert resp_sem.status_code == 200
    assert len(resp_sem.json()["data"]["items"]) == 1


@pytest.mark.asyncio
async def test_api_get_course_by_id_and_tenant_isolation(
    client: AsyncClient,
    student_a: User,
    student_b: User,
) -> None:
    headers_a = make_auth_headers(student_a.id)
    headers_b = make_auth_headers(student_b.id)

    create_resp = await client.post(
        "/api/v1/courses",
        json={"name": "Alice Secret Course"},
        headers=headers_a,
    )
    course_id = create_resp.json()["data"]["id"]

    # Student A can retrieve own course
    resp_own = await client.get(f"/api/v1/courses/{course_id}", headers=headers_a)
    assert resp_own.status_code == 200
    assert resp_own.json()["data"]["id"] == course_id

    # Student B receives 404 Not Found (tenant isolation without leaking existence)
    resp_other = await client.get(f"/api/v1/courses/{course_id}", headers=headers_b)
    assert resp_other.status_code == 404
    assert resp_other.json()["error"]["message"] == "Course not found."


@pytest.mark.asyncio
async def test_api_put_and_patch_course(client: AsyncClient, student_a: User) -> None:
    headers = make_auth_headers(student_a.id)
    create_resp = await client.post(
        "/api/v1/courses",
        json={"name": "Original Name", "course_code": "ORIG101"},
        headers=headers,
    )
    course_id = create_resp.json()["data"]["id"]

    # PUT full update
    put_resp = await client.put(
        f"/api/v1/courses/{course_id}",
        json={"name": "PUT Updated Name", "instructor": "Prof. Turing"},
        headers=headers,
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["data"]["name"] == "PUT Updated Name"
    assert put_resp.json()["data"]["instructor"] == "Prof. Turing"

    # PATCH partial update
    patch_resp = await client.patch(
        f"/api/v1/courses/{course_id}",
        json={"description": "Updated via PATCH"},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["data"]["name"] == "PUT Updated Name"
    assert patch_resp.json()["data"]["description"] == "Updated via PATCH"


@pytest.mark.asyncio
async def test_api_update_another_user_course_404(
    client: AsyncClient,
    student_a: User,
    student_b: User,
) -> None:
    headers_a = make_auth_headers(student_a.id)
    headers_b = make_auth_headers(student_b.id)

    create_resp = await client.post(
        "/api/v1/courses",
        json={"name": "Student A Course"},
        headers=headers_a,
    )
    course_id = create_resp.json()["data"]["id"]

    # Student B attempts PUT
    put_resp = await client.put(
        f"/api/v1/courses/{course_id}",
        json={"name": "Hacked Name"},
        headers=headers_b,
    )
    assert put_resp.status_code == 404

    # Student B attempts PATCH
    patch_resp = await client.patch(
        f"/api/v1/courses/{course_id}",
        json={"name": "Hacked Name"},
        headers=headers_b,
    )
    assert patch_resp.status_code == 404


@pytest.mark.asyncio
async def test_api_archive_course_endpoint(client: AsyncClient, student_a: User) -> None:
    headers = make_auth_headers(student_a.id)
    create_resp = await client.post(
        "/api/v1/courses",
        json={"name": "Course to Archive"},
        headers=headers,
    )
    course_id = create_resp.json()["data"]["id"]

    archive_resp = await client.post(
        f"/api/v1/courses/{course_id}/archive",
        headers=headers,
    )
    assert archive_resp.status_code == 200
    assert archive_resp.json()["data"]["status"] == "archived"


@pytest.mark.asyncio
async def test_api_delete_course_endpoint(
    client: AsyncClient,
    student_a: User,
    student_b: User,
) -> None:
    headers_a = make_auth_headers(student_a.id)
    headers_b = make_auth_headers(student_b.id)

    create_resp = await client.post(
        "/api/v1/courses",
        json={"name": "Course to Delete"},
        headers=headers_a,
    )
    course_id = create_resp.json()["data"]["id"]

    # Student B cannot delete Student A's course
    del_other = await client.delete(f"/api/v1/courses/{course_id}", headers=headers_b)
    assert del_other.status_code == 404

    # Student A can delete own course
    del_resp = await client.delete(f"/api/v1/courses/{course_id}", headers=headers_a)
    assert del_resp.status_code == 200
    assert del_resp.json()["success"] is True

    # Subsequent GET returns 404
    get_resp = await client.get(f"/api/v1/courses/{course_id}", headers=headers_a)
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_api_unauthenticated_request_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/courses")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_inactive_user_403(client: AsyncClient, inactive_student: User) -> None:
    headers = make_auth_headers(inactive_student.id)
    resp = await client.get("/api/v1/courses", headers=headers)
    assert resp.status_code == 403
