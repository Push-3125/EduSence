"""Comprehensive tests for assignments CRUD repository, service, and router (US 2.6)."""

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

from app.api.v1.assignments import router as assignments_router
from app.core.security import create_access_token, hash_password
from app.main import app
from app.repositories.assignment_repo import (
    Assignment,
    AssignmentRepository,
    get_assignment_repo,
)
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
from app.services.assignment_service import (
    AssignmentNotFoundError,
    AssignmentService,
    AssignmentValidationError,
    CourseNotFoundError,
    get_assignment_service,
)

# Ensure assignments router is mounted on app
if not any(
    getattr(route, "path", "").startswith("/api/v1/assignments") for route in app.routes
):
    app.include_router(assignments_router)


# ---------------------------------------------------------------------------
# Test Doubles: In-Memory Fake Repositories
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for authentication in assignments testing."""

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
    """In-memory CourseRepository double for assignment tests."""

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

    async def get_by_id(self, course_id: uuid.UUID) -> Course | None:
        return self.courses.get(course_id)

    async def get_by_id_and_user(
        self,
        course_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Course | None:
        course = self.courses.get(course_id)
        if course and course.user_id == user_id:
            return course
        return None


class FakeAssignmentRepository(AssignmentRepository):
    """In-memory AssignmentRepository double enforcing course ownership."""

    def __init__(self, course_repo: FakeCourseRepository) -> None:
        self.assignments: dict[uuid.UUID, Assignment] = {}
        self.course_repo = course_repo

    async def create(
        self,
        course_id: uuid.UUID,
        title: str,
        description: str | None = None,
        due_at: datetime | None = None,
        priority: str = "medium",
        status: str = "pending",
        completed_at: datetime | None = None,
    ) -> Assignment:
        assignment_id = uuid.uuid4()
        now = datetime.now(UTC)
        if status == "completed":
            if completed_at is None:
                completed_at = now
        else:
            completed_at = None

        assignment = Assignment(
            id=assignment_id,
            course_id=course_id,
            title=title.strip(),
            description=description.strip() if description else None,
            due_at=due_at,
            priority=priority,
            status=status,
            completed_at=completed_at,
            created_at=now,
            updated_at=now,
        )
        self.assignments[assignment_id] = assignment
        return assignment

    async def get_by_id(self, assignment_id: uuid.UUID) -> Assignment | None:
        return self.assignments.get(assignment_id)

    async def get_by_id_and_user(
        self,
        assignment_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Assignment | None:
        assignment = self.assignments.get(assignment_id)
        if not assignment:
            return None
        course = await self.course_repo.get_by_id_and_user(assignment.course_id, user_id)
        if not course:
            return None
        return assignment

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        status: str | None = None,
        priority: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Assignment], int]:
        matched: list[Assignment] = []
        for a in self.assignments.values():
            course = await self.course_repo.get_by_id_and_user(a.course_id, user_id)
            if not course:
                continue
            if course_id is not None and a.course_id != course_id:
                continue
            if status is not None and a.status != status:
                continue
            if priority is not None and a.priority != priority:
                continue
            matched.append(a)

        matched.sort(
            key=lambda x: (
                x.due_at is None,
                x.due_at if x.due_at is not None else datetime.max.replace(tzinfo=UTC),
                -x.created_at.timestamp() if x.created_at else 0,
            )
        )
        total = len(matched)
        paginated = matched[skip : skip + limit]
        return paginated, total

    async def update(self, assignment: Assignment, **kwargs: Any) -> Assignment:
        allowed_fields = {
            "title",
            "description",
            "due_at",
            "priority",
            "status",
            "completed_at",
            "course_id",
        }
        if "status" in kwargs:
            new_status = kwargs["status"]
            if new_status == "completed":
                if "completed_at" not in kwargs or kwargs["completed_at"] is None:
                    kwargs["completed_at"] = datetime.now(UTC)
            else:
                kwargs["completed_at"] = None

        for key, value in kwargs.items():
            if key in allowed_fields:
                setattr(assignment, key, value)

        assignment.updated_at = datetime.now(UTC)
        return assignment

    async def delete(self, assignment: Assignment) -> None:
        self.assignments.pop(assignment.id, None)


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
def fake_assignment_repo(fake_course_repo: FakeCourseRepository) -> FakeAssignmentRepository:
    return FakeAssignmentRepository(course_repo=fake_course_repo)


@pytest.fixture
def assignment_service(
    fake_assignment_repo: FakeAssignmentRepository,
    fake_course_repo: FakeCourseRepository,
) -> AssignmentService:
    return AssignmentService(
        assignment_repo=fake_assignment_repo,
        course_repo=fake_course_repo,
    )


@pytest_asyncio.fixture
async def student_a(fake_user_repo: FakeUserRepository) -> User:
    """Pre-create primary active student."""
    return await fake_user_repo.create_user(
        email="alice.student@university.edu",
        password_hash=hash_password("ValidPass123!"),
        full_name="Alice Student",
    )


@pytest_asyncio.fixture
async def student_b(fake_user_repo: FakeUserRepository) -> User:
    """Pre-create secondary active student for tenant isolation checks."""
    return await fake_user_repo.create_user(
        email="bob.student@university.edu",
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
    fake_assignment_repo: FakeAssignmentRepository,
) -> AsyncGenerator[AsyncClient, None]:
    """Test HTTP client with dependency overrides for repositories and services."""
    service = AssignmentService(
        assignment_repo=fake_assignment_repo,
        course_repo=fake_course_repo,
    )
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_course_repo] = lambda: fake_course_repo
    app.dependency_overrides[get_assignment_repo] = lambda: fake_assignment_repo
    app.dependency_overrides[get_assignment_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_header(user_id: uuid.UUID, expires_seconds: int = 3600) -> dict[str, str]:
    """Generate Bearer Authorization header for user UUID."""
    token = create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(seconds=expires_seconds),
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Repository Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_create_assignment(
    fake_assignment_repo: FakeAssignmentRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Algorithms")
    due = datetime.now(UTC) + timedelta(days=7)

    assignment = await fake_assignment_repo.create(
        course_id=course.id,
        title="Lab 1: Sorting Algorithms",
        description="Implement QuickSort and MergeSort",
        due_at=due,
        priority="high",
        status="pending",
    )
    assert assignment.id is not None
    assert assignment.course_id == course.id
    assert assignment.title == "Lab 1: Sorting Algorithms"
    assert assignment.description == "Implement QuickSort and MergeSort"
    assert assignment.priority == "high"
    assert assignment.status == "pending"
    assert assignment.completed_at is None


@pytest.mark.asyncio
async def test_repo_create_assignment_completed_status(
    fake_assignment_repo: FakeAssignmentRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Databases")
    assignment = await fake_assignment_repo.create(
        course_id=course.id,
        title="HW 1",
        status="completed",
    )
    assert assignment.status == "completed"
    assert assignment.completed_at is not None


@pytest.mark.asyncio
async def test_repo_get_by_id_and_user_found(
    fake_assignment_repo: FakeAssignmentRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Physics")
    assignment = await fake_assignment_repo.create(
        course_id=course.id,
        title="Lab 2",
    )
    found = await fake_assignment_repo.get_by_id_and_user(assignment.id, user_id)
    assert found is not None
    assert found.id == assignment.id


@pytest.mark.asyncio
async def test_repo_get_by_id_and_user_isolation(
    fake_assignment_repo: FakeAssignmentRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    course_a = await fake_course_repo.create(user_id=user_a, name="Math")
    assignment = await fake_assignment_repo.create(course_id=course_a.id, title="Problem Set 1")

    # user_b attempts to fetch user_a's assignment
    found = await fake_assignment_repo.get_by_id_and_user(assignment.id, user_b)
    assert found is None


@pytest.mark.asyncio
async def test_repo_list_by_user_filters_and_pagination(
    fake_assignment_repo: FakeAssignmentRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course_1 = await fake_course_repo.create(user_id=user_id, name="Course 1")
    course_2 = await fake_course_repo.create(user_id=user_id, name="Course 2")

    now = datetime.now(UTC)
    # Add 3 assignments to course 1
    await fake_assignment_repo.create(
        course_id=course_1.id,
        title="Task 1",
        priority="high",
        status="pending",
        due_at=now + timedelta(days=2),
    )
    await fake_assignment_repo.create(
        course_id=course_1.id,
        title="Task 2",
        priority="low",
        status="completed",
        due_at=now + timedelta(days=1),
    )
    await fake_assignment_repo.create(
        course_id=course_1.id,
        title="Task 3",
        priority="medium",
        status="in_progress",
        due_at=now + timedelta(days=3),
    )
    # Add 1 assignment to course 2
    await fake_assignment_repo.create(
        course_id=course_2.id,
        title="Task 4",
        priority="high",
        status="pending",
    )

    # All for user
    items, total = await fake_assignment_repo.list_by_user(user_id=user_id)
    assert total == 4
    assert len(items) == 4

    # Filter by course 1
    items_c1, total_c1 = await fake_assignment_repo.list_by_user(
        user_id=user_id, course_id=course_1.id
    )
    assert total_c1 == 3
    assert len(items_c1) == 3

    # Filter by status 'completed'
    items_comp, total_comp = await fake_assignment_repo.list_by_user(
        user_id=user_id, status="completed"
    )
    assert total_comp == 1
    assert items_comp[0].title == "Task 2"

    # Filter by priority 'high'
    items_high, total_high = await fake_assignment_repo.list_by_user(
        user_id=user_id, priority="high"
    )
    assert total_high == 2
    assert len(items_high) == 2

    # Pagination
    items_paged, total_paged = await fake_assignment_repo.list_by_user(
        user_id=user_id, skip=1, limit=2
    )
    assert total_paged == 4
    assert len(items_paged) == 2


@pytest.mark.asyncio
async def test_repo_update_completed_at_lifecycle(
    fake_assignment_repo: FakeAssignmentRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="AI")
    assignment = await fake_assignment_repo.create(
        course_id=course.id,
        title="Project",
        status="pending",
    )
    assert assignment.completed_at is None

    # Update to completed
    updated = await fake_assignment_repo.update(assignment, status="completed")
    assert updated.status == "completed"
    assert updated.completed_at is not None

    # Update back to in_progress -> completed_at must be cleared
    updated2 = await fake_assignment_repo.update(assignment, status="in_progress")
    assert updated2.status == "in_progress"
    assert updated2.completed_at is None


@pytest.mark.asyncio
async def test_repo_delete(
    fake_assignment_repo: FakeAssignmentRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Art")
    assignment = await fake_assignment_repo.create(course_id=course.id, title="Sketch")

    await fake_assignment_repo.delete(assignment)
    assert await fake_assignment_repo.get_by_id_and_user(assignment.id, user_id) is None


# ---------------------------------------------------------------------------
# Service Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_create_assignment_success(
    assignment_service: AssignmentService,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Biology")
    dto = await assignment_service.create_assignment(
        user_id=user_id,
        course_id=course.id,
        title="Cell Structure Lab",
        description="Read chapter 4 and answer questions",
        priority="high",
        status="pending",
    )
    assert dto.title == "Cell Structure Lab"
    assert dto.course_id == course.id
    assert dto.priority == "high"
    assert dto.status == "pending"


@pytest.mark.asyncio
async def test_service_create_assignment_course_not_found(
    assignment_service: AssignmentService,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    course_a = await fake_course_repo.create(user_id=user_a, name="Chemistry")

    # user_b attempts to create assignment under user_a's course
    with pytest.raises(CourseNotFoundError):
        await assignment_service.create_assignment(
            user_id=user_b,
            course_id=course_a.id,
            title="Lab report",
        )

    # Non-existent course
    with pytest.raises(CourseNotFoundError):
        await assignment_service.create_assignment(
            user_id=user_b,
            course_id=uuid.uuid4(),
            title="Lab report",
        )


@pytest.mark.asyncio
async def test_service_create_assignment_validation_error(
    assignment_service: AssignmentService,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Economics")

    with pytest.raises(AssignmentValidationError):
        await assignment_service.create_assignment(
            user_id=user_id,
            course_id=course.id,
            title="   ",
        )

    with pytest.raises(AssignmentValidationError):
        await assignment_service.create_assignment(
            user_id=user_id,
            course_id=course.id,
            title="Essay 1",
            priority="critical",  # invalid priority
        )

    with pytest.raises(AssignmentValidationError):
        await assignment_service.create_assignment(
            user_id=user_id,
            course_id=course.id,
            title="Essay 1",
            status="finished",  # invalid status
        )


@pytest.mark.asyncio
async def test_service_get_assignment_not_found(
    assignment_service: AssignmentService,
) -> None:
    user_id = uuid.uuid4()
    with pytest.raises(AssignmentNotFoundError):
        await assignment_service.get_assignment(uuid.uuid4(), user_id)


@pytest.mark.asyncio
async def test_service_update_assignment_reassign_course_ownership(
    assignment_service: AssignmentService,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    course_a1 = await fake_course_repo.create(user_id=user_a, name="Course A1")
    course_a2 = await fake_course_repo.create(user_id=user_a, name="Course A2")
    course_b = await fake_course_repo.create(user_id=user_b, name="Course B")

    dto = await assignment_service.create_assignment(
        user_id=user_a,
        course_id=course_a1.id,
        title="Initial Task",
    )

    # Reassigning to another course owned by user_a succeeds
    updated = await assignment_service.update_assignment(
        assignment_id=dto.id,
        user_id=user_a,
        course_id=course_a2.id,
    )
    assert updated.course_id == course_a2.id

    # Reassigning to course_b owned by user_b fails with CourseNotFoundError
    with pytest.raises(CourseNotFoundError):
        await assignment_service.update_assignment(
            assignment_id=dto.id,
            user_id=user_a,
            course_id=course_b.id,
        )


@pytest.mark.asyncio
async def test_service_delete_assignment_not_found(
    assignment_service: AssignmentService,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    course_a = await fake_course_repo.create(user_id=user_a, name="Philosophy")
    dto = await assignment_service.create_assignment(
        user_id=user_a,
        course_id=course_a.id,
        title="Essay",
    )

    # user_b attempts to delete user_a's assignment
    with pytest.raises(AssignmentNotFoundError):
        await assignment_service.delete_assignment(
            assignment_id=dto.id,
            user_id=user_b,
        )


# ---------------------------------------------------------------------------
# API Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_create_assignment_201(
    client: AsyncClient,
    student_a: User,
    fake_course_repo: FakeCourseRepository,
) -> None:
    course = await fake_course_repo.create(user_id=student_a.id, name="Software Architecture")
    headers = make_auth_header(student_a.id)
    payload = {
        "course_id": str(course.id),
        "title": "Module 1 Milestone",
        "description": "Deliver system architecture diagram",
        "priority": "high",
        "status": "pending",
        "due_at": "2026-11-01T23:59:59Z",
    }
    resp = await client.post("/api/v1/assignments", json=payload, headers=headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["title"] == "Module 1 Milestone"
    assert body["data"]["priority"] == "high"
    assert body["data"]["status"] == "pending"
    assert body["data"]["course_id"] == str(course.id)


@pytest.mark.asyncio
async def test_api_create_assignment_under_another_student_course_404(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_course_repo: FakeCourseRepository,
) -> None:
    # student_a creates course
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Private Course A")

    # student_b attempts to create an assignment under student_a's course
    headers_b = make_auth_header(student_b.id)
    payload = {
        "course_id": str(course_a.id),
        "title": "Hacked Assignment",
        "priority": "medium",
    }
    resp = await client.post("/api/v1/assignments", json=payload, headers=headers_b)
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_404"


@pytest.mark.asyncio
async def test_api_create_assignment_validation_422(
    client: AsyncClient,
    student_a: User,
    fake_course_repo: FakeCourseRepository,
) -> None:
    course = await fake_course_repo.create(user_id=student_a.id, name="Test Course")
    headers = make_auth_header(student_a.id)

    # Empty title
    resp1 = await client.post(
        "/api/v1/assignments",
        json={"course_id": str(course.id), "title": "   "},
        headers=headers,
    )
    assert resp1.status_code == 422

    # Invalid priority
    resp2 = await client.post(
        "/api/v1/assignments",
        json={"course_id": str(course.id), "title": "Task", "priority": "super_urgent"},
        headers=headers,
    )
    assert resp2.status_code == 422

    # Invalid status
    resp3 = await client.post(
        "/api/v1/assignments",
        json={"course_id": str(course.id), "title": "Task", "status": "resolved"},
        headers=headers,
    )
    assert resp3.status_code == 422


@pytest.mark.asyncio
async def test_api_list_assignments_tenant_isolation(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Course A")
    course_b = await fake_course_repo.create(user_id=student_b.id, name="Course B")

    await fake_assignment_repo.create(course_id=course_a.id, title="Assignment A1")
    await fake_assignment_repo.create(course_id=course_a.id, title="Assignment A2")
    await fake_assignment_repo.create(course_id=course_b.id, title="Assignment B1")

    # Student A request
    headers_a = make_auth_header(student_a.id)
    resp_a = await client.get("/api/v1/assignments", headers=headers_a)
    assert resp_a.status_code == 200
    data_a = resp_a.json()["data"]
    assert data_a["total"] == 2
    titles_a = [item["title"] for item in data_a["items"]]
    assert "Assignment A1" in titles_a
    assert "Assignment A2" in titles_a
    assert "Assignment B1" not in titles_a

    # Student B request
    headers_b = make_auth_header(student_b.id)
    resp_b = await client.get("/api/v1/assignments", headers=headers_b)
    assert resp_b.status_code == 200
    data_b = resp_b.json()["data"]
    assert data_b["total"] == 1
    assert data_b["items"][0]["title"] == "Assignment B1"


@pytest.mark.asyncio
async def test_api_list_assignments_filters(
    client: AsyncClient,
    student_a: User,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    course1 = await fake_course_repo.create(user_id=student_a.id, name="Course 1")
    course2 = await fake_course_repo.create(user_id=student_a.id, name="Course 2")

    await fake_assignment_repo.create(
        course_id=course1.id, title="Asg 1", priority="low", status="pending"
    )
    await fake_assignment_repo.create(
        course_id=course1.id, title="Asg 2", priority="high", status="completed"
    )
    await fake_assignment_repo.create(
        course_id=course2.id, title="Asg 3", priority="high", status="in_progress"
    )

    headers = make_auth_header(student_a.id)

    # Filter by course
    resp = await client.get(f"/api/v1/assignments?course_id={course1.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 2

    # Filter by status
    resp = await client.get("/api/v1/assignments?status=completed", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 1
    assert resp.json()["data"]["items"][0]["title"] == "Asg 2"

    # Filter by priority
    resp = await client.get("/api/v1/assignments?priority=high", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 2


@pytest.mark.asyncio
async def test_api_get_assignment_by_id_success_and_isolation(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Course A")
    asg = await fake_assignment_repo.create(course_id=course_a.id, title="Assignment A")

    headers_a = make_auth_header(student_a.id)
    headers_b = make_auth_header(student_b.id)

    # Student A can view
    resp_a = await client.get(f"/api/v1/assignments/{asg.id}", headers=headers_a)
    assert resp_a.status_code == 200
    assert resp_a.json()["data"]["title"] == "Assignment A"

    # Student B cannot view (404)
    resp_b = await client.get(f"/api/v1/assignments/{asg.id}", headers=headers_b)
    assert resp_b.status_code == 404
    assert resp_b.json()["error"]["code"] == "HTTP_404"


@pytest.mark.asyncio
async def test_api_update_assignment_put_and_patch(
    client: AsyncClient,
    student_a: User,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    course = await fake_course_repo.create(user_id=student_a.id, name="Course")
    asg = await fake_assignment_repo.create(
        course_id=course.id, title="Initial Title", status="pending"
    )
    headers = make_auth_header(student_a.id)

    # PUT update status to completed -> completed_at set
    put_resp = await client.put(
        f"/api/v1/assignments/{asg.id}",
        json={"title": "Updated Title", "status": "completed"},
        headers=headers,
    )
    assert put_resp.status_code == 200
    data = put_resp.json()["data"]
    assert data["title"] == "Updated Title"
    assert data["status"] == "completed"
    assert data["completed_at"] is not None

    # PATCH update status back to in_progress -> completed_at cleared
    patch_resp = await client.patch(
        f"/api/v1/assignments/{asg.id}",
        json={"status": "in_progress"},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    data2 = patch_resp.json()["data"]
    assert data2["status"] == "in_progress"
    assert data2["completed_at"] is None


@pytest.mark.asyncio
async def test_api_update_other_student_assignment_404(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Course A")
    asg = await fake_assignment_repo.create(course_id=course_a.id, title="Asg A")

    headers_b = make_auth_header(student_b.id)
    resp = await client.put(
        f"/api/v1/assignments/{asg.id}",
        json={"title": "Malicious Update"},
        headers=headers_b,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_update_assignment_reassign_to_other_student_course_404(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Course A")
    course_b = await fake_course_repo.create(user_id=student_b.id, name="Course B")
    asg = await fake_assignment_repo.create(course_id=course_a.id, title="Asg A")

    headers_a = make_auth_header(student_a.id)
    # student_a tries to reassign assignment to student_b's course
    resp = await client.put(
        f"/api/v1/assignments/{asg.id}",
        json={"course_id": str(course_b.id)},
        headers=headers_a,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "Course not found."


@pytest.mark.asyncio
async def test_api_delete_assignment_endpoint(
    client: AsyncClient,
    student_a: User,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    course = await fake_course_repo.create(user_id=student_a.id, name="Course")
    asg = await fake_assignment_repo.create(course_id=course.id, title="Task To Delete")
    headers = make_auth_header(student_a.id)

    # Delete succeeds
    del_resp = await client.delete(f"/api/v1/assignments/{asg.id}", headers=headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["success"] is True

    # Subsequent GET returns 404
    get_resp = await client.get(f"/api/v1/assignments/{asg.id}", headers=headers)
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_api_delete_other_student_assignment_404(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Course A")
    asg = await fake_assignment_repo.create(course_id=course_a.id, title="Asg A")

    headers_b = make_auth_header(student_b.id)
    resp = await client.delete(f"/api/v1/assignments/{asg.id}", headers=headers_b)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_unauthenticated_request_401(
    client: AsyncClient,
) -> None:
    random_id = uuid.uuid4()
    resp_get = await client.get("/api/v1/assignments")
    assert resp_get.status_code == 401

    resp_post = await client.post("/api/v1/assignments", json={"title": "Test"})
    assert resp_post.status_code == 401

    resp_put = await client.put(f"/api/v1/assignments/{random_id}", json={"title": "Test"})
    assert resp_put.status_code == 401

    resp_del = await client.delete(f"/api/v1/assignments/{random_id}")
    assert resp_del.status_code == 401


@pytest.mark.asyncio
async def test_api_inactive_user_403(
    client: AsyncClient,
    inactive_student: User,
) -> None:
    headers = make_auth_header(inactive_student.id)
    resp = await client.get("/api/v1/assignments", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "HTTP_403"
