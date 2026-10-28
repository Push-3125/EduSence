"""Comprehensive tests for exams schedule tracking repository, service, and router (US 2.7)."""

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

from app.api.v1.exams import router as exams_router
from app.core.security import create_access_token, hash_password
from app.main import app
from app.repositories.course_repo import (
    Course,
    CourseRepository,
    get_course_repo,
)
from app.repositories.exam_repo import (
    Exam,
    ExamRepository,
    get_exam_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.course_service import CourseNotFoundError
from app.services.exam_service import (
    ExamNotFoundError,
    ExamService,
    ExamValidationError,
    get_exam_service,
)

# Ensure exams router is mounted on app
if not any(
    getattr(route, "path", "").startswith("/api/v1/exams") for route in app.routes
):
    app.include_router(exams_router)


# ---------------------------------------------------------------------------
# Test Doubles: In-Memory Fake Repositories
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for authentication in exam testing."""

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
    """In-memory CourseRepository double for exam tests."""

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


class FakeExamRepository(ExamRepository):
    """In-memory ExamRepository double enforcing course ownership."""

    def __init__(self, course_repo: FakeCourseRepository) -> None:
        self.exams: dict[uuid.UUID, Exam] = {}
        self.course_repo = course_repo

    async def create(
        self,
        course_id: uuid.UUID,
        title: str,
        exam_at: datetime,
        duration_minutes: int | None = None,
        location: str | None = None,
        notes: str | None = None,
    ) -> Exam:
        exam_id = uuid.uuid4()
        now = datetime.now(UTC)
        exam = Exam(
            id=exam_id,
            course_id=course_id,
            title=title.strip(),
            exam_at=exam_at,
            duration_minutes=duration_minutes,
            location=location.strip() if location else None,
            notes=notes.strip() if notes else None,
            created_at=now,
            updated_at=now,
        )
        self.exams[exam_id] = exam
        return exam

    async def get_by_id(self, exam_id: uuid.UUID) -> Exam | None:
        return self.exams.get(exam_id)

    async def get_by_id_and_user(
        self,
        exam_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Exam | None:
        exam = self.exams.get(exam_id)
        if not exam:
            return None
        course = await self.course_repo.get_by_id_and_user(exam.course_id, user_id)
        if not course:
            return None
        return exam

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        upcoming_only: bool = False,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Exam], int]:
        matched: list[Exam] = []
        now = datetime.now(UTC)
        for e in self.exams.values():
            course = await self.course_repo.get_by_id_and_user(e.course_id, user_id)
            if not course:
                continue
            if course_id is not None and e.course_id != course_id:
                continue
            if upcoming_only and e.exam_at < now:
                continue
            matched.append(e)

        matched.sort(
            key=lambda x: (
                x.exam_at,
                -x.created_at.timestamp() if x.created_at else 0,
            )
        )
        total = len(matched)
        paginated = matched[skip : skip + limit]
        return paginated, total

    async def update(self, exam: Exam, **kwargs: Any) -> Exam:
        allowed_fields = {
            "course_id",
            "title",
            "exam_at",
            "duration_minutes",
            "location",
            "notes",
        }
        for key, value in kwargs.items():
            if key in allowed_fields:
                setattr(exam, key, value)
        exam.updated_at = datetime.now(UTC)
        return exam

    async def delete(self, exam: Exam) -> None:
        self.exams.pop(exam.id, None)


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
def fake_exam_repo(fake_course_repo: FakeCourseRepository) -> FakeExamRepository:
    return FakeExamRepository(course_repo=fake_course_repo)


@pytest.fixture
def exam_service(
    fake_exam_repo: FakeExamRepository,
    fake_course_repo: FakeCourseRepository,
) -> ExamService:
    return ExamService(
        exam_repo=fake_exam_repo,
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
    fake_exam_repo: FakeExamRepository,
) -> AsyncGenerator[AsyncClient, None]:
    """Test HTTP client with dependency overrides for repositories and services."""
    service = ExamService(
        exam_repo=fake_exam_repo,
        course_repo=fake_course_repo,
    )
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_course_repo] = lambda: fake_course_repo
    app.dependency_overrides[get_exam_repo] = lambda: fake_exam_repo
    app.dependency_overrides[get_exam_service] = lambda: service

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
async def test_repo_create_exam(
    fake_exam_repo: FakeExamRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Mathematics")
    exam_time = datetime.now(UTC) + timedelta(days=14)

    exam = await fake_exam_repo.create(
        course_id=course.id,
        title="Final Calculus Exam",
        exam_at=exam_time,
        duration_minutes=120,
        location="Room 301, Building B",
        notes="Bring scientific calculator and student ID",
    )
    assert exam.id is not None
    assert exam.course_id == course.id
    assert exam.title == "Final Calculus Exam"
    assert exam.exam_at == exam_time
    assert exam.duration_minutes == 120
    assert exam.location == "Room 301, Building B"
    assert exam.notes == "Bring scientific calculator and student ID"
    assert exam.created_at is not None
    assert exam.updated_at is not None


@pytest.mark.asyncio
async def test_repo_get_by_id_and_user_found(
    fake_exam_repo: FakeExamRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Physics")
    exam_time = datetime.now(UTC) + timedelta(days=5)
    exam = await fake_exam_repo.create(
        course_id=course.id,
        title="Midterm Physics",
        exam_at=exam_time,
    )

    found = await fake_exam_repo.get_by_id_and_user(exam.id, user_id)
    assert found is not None
    assert found.id == exam.id
    assert found.title == "Midterm Physics"


@pytest.mark.asyncio
async def test_repo_get_by_id_and_user_isolation(
    fake_exam_repo: FakeExamRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_alice = uuid.uuid4()
    user_bob = uuid.uuid4()
    course_alice = await fake_course_repo.create(user_id=user_alice, name="Chemistry")
    exam_alice = await fake_exam_repo.create(
        course_id=course_alice.id,
        title="Chemistry Lab Practical",
        exam_at=datetime.now(UTC) + timedelta(days=3),
    )

    # Bob attempts to access Alice's exam -> must return None
    found_by_bob = await fake_exam_repo.get_by_id_and_user(exam_alice.id, user_bob)
    assert found_by_bob is None


@pytest.mark.asyncio
async def test_repo_list_by_user_filters_and_pagination(
    fake_exam_repo: FakeExamRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course1 = await fake_course_repo.create(user_id=user_id, name="Biology")
    course2 = await fake_course_repo.create(user_id=user_id, name="History")

    now = datetime.now(UTC)
    # Past exam
    past_exam = await fake_exam_repo.create(
        course_id=course1.id,
        title="Past Biology Quiz",
        exam_at=now - timedelta(days=2),
    )
    # Upcoming exams
    upcoming_exam1 = await fake_exam_repo.create(
        course_id=course1.id,
        title="Upcoming Biology Midterm",
        exam_at=now + timedelta(days=5),
    )
    upcoming_exam2 = await fake_exam_repo.create(
        course_id=course2.id,
        title="Upcoming History Final",
        exam_at=now + timedelta(days=10),
    )

    # 1. List all for user (no filter)
    all_exams, total = await fake_exam_repo.list_by_user(user_id=user_id)
    assert total == 3
    assert len(all_exams) == 3

    # 2. Filter upcoming_only=True
    upcoming_exams, total_upcoming = await fake_exam_repo.list_by_user(
        user_id=user_id, upcoming_only=True
    )
    assert total_upcoming == 2
    assert past_exam.id not in [e.id for e in upcoming_exams]
    assert upcoming_exam1.id in [e.id for e in upcoming_exams]
    assert upcoming_exam2.id in [e.id for e in upcoming_exams]

    # 3. Filter course_id=course1.id
    c1_exams, total_c1 = await fake_exam_repo.list_by_user(
        user_id=user_id, course_id=course1.id
    )
    assert total_c1 == 2
    assert all(e.course_id == course1.id for e in c1_exams)

    # 4. Filter course_id=course1.id and upcoming_only=True
    c1_upcoming, total_c1_up = await fake_exam_repo.list_by_user(
        user_id=user_id, course_id=course1.id, upcoming_only=True
    )
    assert total_c1_up == 1
    assert c1_upcoming[0].id == upcoming_exam1.id

    # 5. Pagination offset/limit
    paginated, total = await fake_exam_repo.list_by_user(
        user_id=user_id, skip=1, limit=1
    )
    assert total == 3
    assert len(paginated) == 1


@pytest.mark.asyncio
async def test_repo_update_exam(
    fake_exam_repo: FakeExamRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Databases")
    exam = await fake_exam_repo.create(
        course_id=course.id,
        title="SQL Midterm",
        exam_at=datetime.now(UTC) + timedelta(days=7),
        duration_minutes=60,
    )

    new_time = datetime.now(UTC) + timedelta(days=8)
    updated = await fake_exam_repo.update(
        exam,
        title="Advanced SQL Midterm",
        exam_at=new_time,
        duration_minutes=90,
        location="Hall 101",
        notes="Open book exam",
    )
    assert updated.title == "Advanced SQL Midterm"
    assert updated.exam_at == new_time
    assert updated.duration_minutes == 90
    assert updated.location == "Hall 101"
    assert updated.notes == "Open book exam"


@pytest.mark.asyncio
async def test_repo_delete_exam(
    fake_exam_repo: FakeExamRepository,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Networking")
    exam = await fake_exam_repo.create(
        course_id=course.id,
        title="CCNA Quiz",
        exam_at=datetime.now(UTC) + timedelta(days=4),
    )

    await fake_exam_repo.delete(exam)
    assert await fake_exam_repo.get_by_id(exam.id) is None


# ---------------------------------------------------------------------------
# Service Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_create_exam_success(
    exam_service: ExamService,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Operating Systems")
    exam_time = datetime.now(UTC) + timedelta(days=10)

    dto = await exam_service.create_exam(
        user_id=user_id,
        course_id=course.id,
        title="OS Final Exam",
        exam_at=exam_time,
        duration_minutes=120,
        location="Lab A3",
        notes="Process synchronization & memory management",
    )
    assert dto.id is not None
    assert dto.course_id == course.id
    assert dto.title == "OS Final Exam"
    assert dto.exam_at == exam_time
    assert dto.duration_minutes == 120
    assert dto.location == "Lab A3"
    assert dto.notes == "Process synchronization & memory management"


@pytest.mark.asyncio
async def test_service_create_exam_course_not_found(
    exam_service: ExamService,
    fake_course_repo: FakeCourseRepository,
) -> None:
    alice_id = uuid.uuid4()
    bob_id = uuid.uuid4()
    course_bob = await fake_course_repo.create(user_id=bob_id, name="Bob's Course")

    # Alice tries to create exam under Bob's course -> CourseNotFoundError
    with pytest.raises(CourseNotFoundError):
        await exam_service.create_exam(
            user_id=alice_id,
            course_id=course_bob.id,
            title="Unauthorized Exam",
            exam_at=datetime.now(UTC) + timedelta(days=2),
        )


@pytest.mark.asyncio
async def test_service_create_exam_validation_errors(
    exam_service: ExamService,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_id = uuid.uuid4()
    course = await fake_course_repo.create(user_id=user_id, name="Math")

    # Empty title
    with pytest.raises(ExamValidationError, match="title cannot be empty"):
        await exam_service.create_exam(
            user_id=user_id,
            course_id=course.id,
            title="   ",
            exam_at=datetime.now(UTC) + timedelta(days=1),
        )

    # Title exceeds 255 chars
    with pytest.raises(ExamValidationError, match="cannot exceed 255 characters"):
        await exam_service.create_exam(
            user_id=user_id,
            course_id=course.id,
            title="A" * 256,
            exam_at=datetime.now(UTC) + timedelta(days=1),
        )

    # Duration <= 0
    with pytest.raises(ExamValidationError, match="positive integer"):
        await exam_service.create_exam(
            user_id=user_id,
            course_id=course.id,
            title="Math Exam",
            exam_at=datetime.now(UTC) + timedelta(days=1),
            duration_minutes=0,
        )

    with pytest.raises(ExamValidationError, match="positive integer"):
        await exam_service.create_exam(
            user_id=user_id,
            course_id=course.id,
            title="Math Exam",
            exam_at=datetime.now(UTC) + timedelta(days=1),
            duration_minutes=-15,
        )

    # Location exceeds 255 chars
    with pytest.raises(ExamValidationError, match="location cannot exceed 255 characters"):
        await exam_service.create_exam(
            user_id=user_id,
            course_id=course.id,
            title="Math Exam",
            exam_at=datetime.now(UTC) + timedelta(days=1),
            location="L" * 256,
        )


@pytest.mark.asyncio
async def test_service_get_exam_not_found(
    exam_service: ExamService,
) -> None:
    user_id = uuid.uuid4()
    with pytest.raises(ExamNotFoundError):
        await exam_service.get_exam(exam_id=uuid.uuid4(), user_id=user_id)


@pytest.mark.asyncio
async def test_service_list_exams_pagination_validation(
    exam_service: ExamService,
) -> None:
    user_id = uuid.uuid4()
    with pytest.raises(ExamValidationError, match="skip must be non-negative"):
        await exam_service.list_exams(user_id=user_id, skip=-1)

    with pytest.raises(ExamValidationError, match="limit must be between 1 and 100"):
        await exam_service.list_exams(user_id=user_id, limit=0)

    with pytest.raises(ExamValidationError, match="limit must be between 1 and 100"):
        await exam_service.list_exams(user_id=user_id, limit=101)


@pytest.mark.asyncio
async def test_service_update_exam_reassign_course_ownership(
    exam_service: ExamService,
    fake_course_repo: FakeCourseRepository,
) -> None:
    user_alice = uuid.uuid4()
    user_bob = uuid.uuid4()
    course_alice = await fake_course_repo.create(user_id=user_alice, name="Alice Course")
    course_bob = await fake_course_repo.create(user_id=user_bob, name="Bob Course")

    exam_dto = await exam_service.create_exam(
        user_id=user_alice,
        course_id=course_alice.id,
        title="Alice Exam",
        exam_at=datetime.now(UTC) + timedelta(days=3),
    )

    # Reassigning to Bob's course must fail with CourseNotFoundError
    with pytest.raises(CourseNotFoundError):
        await exam_service.update_exam(
            exam_id=exam_dto.id,
            user_id=user_alice,
            course_id=course_bob.id,
        )


@pytest.mark.asyncio
async def test_service_delete_exam_not_found(
    exam_service: ExamService,
) -> None:
    user_id = uuid.uuid4()
    with pytest.raises(ExamNotFoundError):
        await exam_service.delete_exam(exam_id=uuid.uuid4(), user_id=user_id)


# ---------------------------------------------------------------------------
# API Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_create_exam_201(
    client: AsyncClient,
    student_a: User,
    fake_course_repo: FakeCourseRepository,
) -> None:
    course = await fake_course_repo.create(user_id=student_a.id, name="Distributed Systems")
    exam_at = (datetime.now(UTC) + timedelta(days=10)).isoformat()

    headers = make_auth_header(student_a.id)
    payload = {
        "course_id": str(course.id),
        "title": "Midterm Exam on Raft Consensus",
        "exam_at": exam_at,
        "duration_minutes": 90,
        "location": "Room 204, Tech Hall",
        "notes": "Covers chapters 1-5 and Raft paper",
    }

    response = await client.post("/api/v1/exams", json=payload, headers=headers)
    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["data"]["title"] == "Midterm Exam on Raft Consensus"
    assert body["data"]["duration_minutes"] == 90
    assert body["data"]["location"] == "Room 204, Tech Hall"
    assert body["data"]["notes"] == "Covers chapters 1-5 and Raft paper"
    assert body["data"]["course_id"] == str(course.id)


@pytest.mark.asyncio
async def test_api_create_exam_under_another_student_course_404(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_course_repo: FakeCourseRepository,
) -> None:
    bob_course = await fake_course_repo.create(user_id=student_b.id, name="Bob Secret Course")
    exam_at = (datetime.now(UTC) + timedelta(days=5)).isoformat()

    headers = make_auth_header(student_a.id)
    payload = {
        "course_id": str(bob_course.id),
        "title": "Alice Hijack Attempt",
        "exam_at": exam_at,
    }

    response = await client.post("/api/v1/exams", json=payload, headers=headers)
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_404"


@pytest.mark.asyncio
async def test_api_create_exam_validation_422(
    client: AsyncClient,
    student_a: User,
    fake_course_repo: FakeCourseRepository,
) -> None:
    course = await fake_course_repo.create(user_id=student_a.id, name="Machine Learning")
    headers = make_auth_header(student_a.id)

    # 1. Whitespace title
    payload_bad_title = {
        "course_id": str(course.id),
        "title": "    ",
        "exam_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
    }
    res1 = await client.post("/api/v1/exams", json=payload_bad_title, headers=headers)
    assert res1.status_code == 422
    assert res1.json()["success"] is False

    # 2. Non-positive duration
    payload_bad_duration = {
        "course_id": str(course.id),
        "title": "ML Midterm",
        "exam_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        "duration_minutes": 0,
    }
    res2 = await client.post("/api/v1/exams", json=payload_bad_duration, headers=headers)
    assert res2.status_code == 422
    assert res2.json()["success"] is False

    # 3. Missing exam_at
    payload_missing_date = {
        "course_id": str(course.id),
        "title": "ML Midterm",
    }
    res3 = await client.post("/api/v1/exams", json=payload_missing_date, headers=headers)
    assert res3.status_code == 422


@pytest.mark.asyncio
async def test_api_list_exams_tenant_isolation(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_course_repo: FakeCourseRepository,
    fake_exam_repo: FakeExamRepository,
) -> None:
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Alice Course")
    course_b = await fake_course_repo.create(user_id=student_b.id, name="Bob Course")

    await fake_exam_repo.create(
        course_id=course_a.id,
        title="Alice Exam 1",
        exam_at=datetime.now(UTC) + timedelta(days=3),
    )
    await fake_exam_repo.create(
        course_id=course_b.id,
        title="Bob Secret Exam",
        exam_at=datetime.now(UTC) + timedelta(days=4),
    )

    headers_a = make_auth_header(student_a.id)
    res_a = await client.get("/api/v1/exams", headers=headers_a)
    assert res_a.status_code == 200
    body_a = res_a.json()
    assert body_a["success"] is True
    assert body_a["data"]["total"] == 1
    assert body_a["data"]["items"][0]["title"] == "Alice Exam 1"

    headers_b = make_auth_header(student_b.id)
    res_b = await client.get("/api/v1/exams", headers=headers_b)
    assert res_b.status_code == 200
    body_b = res_b.json()
    assert body_b["success"] is True
    assert body_b["data"]["total"] == 1
    assert body_b["data"]["items"][0]["title"] == "Bob Secret Exam"


@pytest.mark.asyncio
async def test_api_list_exams_filters(
    client: AsyncClient,
    student_a: User,
    fake_course_repo: FakeCourseRepository,
    fake_exam_repo: FakeExamRepository,
) -> None:
    c1 = await fake_course_repo.create(user_id=student_a.id, name="Math")
    c2 = await fake_course_repo.create(user_id=student_a.id, name="English")

    now = datetime.now(UTC)
    past_exam = await fake_exam_repo.create(
        course_id=c1.id,
        title="Past Math Quiz",
        exam_at=now - timedelta(days=2),
    )
    upcoming_math = await fake_exam_repo.create(
        course_id=c1.id,
        title="Upcoming Math Final",
        exam_at=now + timedelta(days=5),
    )
    upcoming_english = await fake_exam_repo.create(
        course_id=c2.id,
        title="Upcoming English Essay Exam",
        exam_at=now + timedelta(days=8),
    )

    headers = make_auth_header(student_a.id)

    # 1. upcoming_only=true
    res_up = await client.get("/api/v1/exams?upcoming_only=true", headers=headers)
    assert res_up.status_code == 200
    data_up = res_up.json()["data"]
    assert data_up["total"] == 2
    titles_up = [item["title"] for item in data_up["items"]]
    assert past_exam.title not in titles_up
    assert upcoming_math.title in titles_up
    assert upcoming_english.title in titles_up

    # 2. course_id filter
    res_c1 = await client.get(f"/api/v1/exams?course_id={c1.id}", headers=headers)
    assert res_c1.status_code == 200
    data_c1 = res_c1.json()["data"]
    assert data_c1["total"] == 2
    assert all(item["course_id"] == str(c1.id) for item in data_c1["items"])

    # 3. upcoming_only=true AND course_id filter
    res_c1_up = await client.get(
        f"/api/v1/exams?course_id={c1.id}&upcoming_only=true", headers=headers
    )
    assert res_c1_up.status_code == 200
    data_c1_up = res_c1_up.json()["data"]
    assert data_c1_up["total"] == 1
    assert data_c1_up["items"][0]["title"] == "Upcoming Math Final"


@pytest.mark.asyncio
async def test_api_get_exam_by_id_success_and_isolation(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_course_repo: FakeCourseRepository,
    fake_exam_repo: FakeExamRepository,
) -> None:
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Alice Chemistry")
    exam_a = await fake_exam_repo.create(
        course_id=course_a.id,
        title="Organic Chemistry Midterm",
        exam_at=datetime.now(UTC) + timedelta(days=6),
        duration_minutes=90,
        location="Lab 102",
    )

    # Alice retrieves her exam -> 200 OK
    headers_a = make_auth_header(student_a.id)
    res_a = await client.get(f"/api/v1/exams/{exam_a.id}", headers=headers_a)
    assert res_a.status_code == 200
    assert res_a.json()["data"]["title"] == "Organic Chemistry Midterm"

    # Bob attempts to retrieve Alice's exam -> 404 NOT FOUND
    headers_b = make_auth_header(student_b.id)
    res_b = await client.get(f"/api/v1/exams/{exam_a.id}", headers=headers_b)
    assert res_b.status_code == 404
    assert res_b.json()["error"]["code"] == "HTTP_404"


@pytest.mark.asyncio
async def test_api_update_exam_put_and_patch(
    client: AsyncClient,
    student_a: User,
    fake_course_repo: FakeCourseRepository,
    fake_exam_repo: FakeExamRepository,
) -> None:
    course = await fake_course_repo.create(user_id=student_a.id, name="Software Engineering")
    exam = await fake_exam_repo.create(
        course_id=course.id,
        title="SE Sprint Exam",
        exam_at=datetime.now(UTC) + timedelta(days=3),
        duration_minutes=60,
        location="Room 101",
    )
    headers = make_auth_header(student_a.id)

    # 1. PUT update
    new_exam_at = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    put_payload = {
        "title": "SE Sprint Final Exam",
        "exam_at": new_exam_at,
        "duration_minutes": 120,
        "location": "Main Auditorium",
        "notes": "Covers Scrum, CI/CD, and Architecture",
    }
    put_res = await client.put(f"/api/v1/exams/{exam.id}", json=put_payload, headers=headers)
    assert put_res.status_code == 200
    put_data = put_res.json()["data"]
    assert put_data["title"] == "SE Sprint Final Exam"
    assert put_data["duration_minutes"] == 120
    assert put_data["location"] == "Main Auditorium"
    assert put_data["notes"] == "Covers Scrum, CI/CD, and Architecture"

    # 2. PATCH update
    patch_payload = {
        "location": "Online via Zoom",
    }
    patch_res = await client.patch(
        f"/api/v1/exams/{exam.id}", json=patch_payload, headers=headers
    )
    assert patch_res.status_code == 200
    patch_data = patch_res.json()["data"]
    assert patch_data["title"] == "SE Sprint Final Exam"
    assert patch_data["location"] == "Online via Zoom"


@pytest.mark.asyncio
async def test_api_update_other_student_exam_404(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_course_repo: FakeCourseRepository,
    fake_exam_repo: FakeExamRepository,
) -> None:
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Course A")
    exam_a = await fake_exam_repo.create(
        course_id=course_a.id,
        title="Exam A",
        exam_at=datetime.now(UTC) + timedelta(days=2),
    )

    headers_b = make_auth_header(student_b.id)
    res = await client.put(
        f"/api/v1/exams/{exam_a.id}",
        json={"title": "Hacked Title"},
        headers=headers_b,
    )
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "HTTP_404"


@pytest.mark.asyncio
async def test_api_update_exam_reassign_to_other_student_course_404(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_course_repo: FakeCourseRepository,
    fake_exam_repo: FakeExamRepository,
) -> None:
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Alice Course")
    course_b = await fake_course_repo.create(user_id=student_b.id, name="Bob Course")
    exam_a = await fake_exam_repo.create(
        course_id=course_a.id,
        title="Alice Exam",
        exam_at=datetime.now(UTC) + timedelta(days=2),
    )

    headers_a = make_auth_header(student_a.id)
    res = await client.patch(
        f"/api/v1/exams/{exam_a.id}",
        json={"course_id": str(course_b.id)},
        headers=headers_a,
    )
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "HTTP_404"


@pytest.mark.asyncio
async def test_api_delete_exam_endpoint(
    client: AsyncClient,
    student_a: User,
    fake_course_repo: FakeCourseRepository,
    fake_exam_repo: FakeExamRepository,
) -> None:
    course = await fake_course_repo.create(user_id=student_a.id, name="Robotics")
    exam = await fake_exam_repo.create(
        course_id=course.id,
        title="Robotics Quiz",
        exam_at=datetime.now(UTC) + timedelta(days=1),
    )
    headers = make_auth_header(student_a.id)

    res = await client.delete(f"/api/v1/exams/{exam.id}", headers=headers)
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert "deleted successfully" in res.json()["message"]

    # Verify deleted
    assert await fake_exam_repo.get_by_id(exam.id) is None


@pytest.mark.asyncio
async def test_api_delete_other_student_exam_404(
    client: AsyncClient,
    student_a: User,
    student_b: User,
    fake_course_repo: FakeCourseRepository,
    fake_exam_repo: FakeExamRepository,
) -> None:
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Alice Robotics")
    exam_a = await fake_exam_repo.create(
        course_id=course_a.id,
        title="Alice Exam",
        exam_at=datetime.now(UTC) + timedelta(days=1),
    )
    headers_b = make_auth_header(student_b.id)

    res = await client.delete(f"/api/v1/exams/{exam_a.id}", headers=headers_b)
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "HTTP_404"


@pytest.mark.asyncio
async def test_api_unauthenticated_request_401(
    client: AsyncClient,
) -> None:
    res = await client.get("/api/v1/exams")
    assert res.status_code == 401
    assert res.json()["success"] is False
    assert res.json()["error"]["code"] == "HTTP_401"


@pytest.mark.asyncio
async def test_api_inactive_user_403(
    client: AsyncClient,
    inactive_student: User,
) -> None:
    headers = make_auth_header(inactive_student.id)
    res = await client.get("/api/v1/exams", headers=headers)
    assert res.status_code == 403
    assert res.json()["success"] is False
    assert res.json()["error"]["code"] == "HTTP_403"
