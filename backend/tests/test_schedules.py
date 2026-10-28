"""Comprehensive tests for weekly recurring class schedules and sessions repository, service, and router (US 2.9)."""

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

# Ensure backend root is on sys.path for direct execution
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.schedules import router as schedules_router
from app.core.security import create_access_token, hash_password
from app.main import app
from app.repositories.course_repo import (
    Course,
    CourseRepository,
    get_course_repo,
)
from app.repositories.schedule_repo import (
    ClassSchedule,
    ClassSession,
    ScheduleRepository,
    get_schedule_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.schedule_service import (
    CourseNotFoundError,
    ScheduleNotFoundError,
    ScheduleService,
    ScheduleValidationError,
    SessionNotFoundError,
    get_schedule_service,
)

# Ensure schedules router is mounted on app
if not any(
    getattr(route, "path", "").startswith("/api/v1/schedules") for route in app.routes
):
    app.include_router(schedules_router)


# ---------------------------------------------------------------------------
# Test Doubles: In-Memory Fake Repositories
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for authentication in schedule tests."""

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
    """In-memory CourseRepository double for schedule tests."""

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


class FakeScheduleRepository(ScheduleRepository):
    """In-memory ScheduleRepository double enforcing student ownership."""

    def __init__(self, course_repo: FakeCourseRepository) -> None:
        self.schedules: dict[uuid.UUID, ClassSchedule] = {}
        self.sessions: dict[uuid.UUID, ClassSession] = {}
        self.course_repo = course_repo

    async def create_schedule(
        self,
        course_id: uuid.UUID,
        day_of_week: int,
        start_time: time,
        end_time: time,
        start_date: date,
        end_date: date,
        location: str | None = None,
    ) -> ClassSchedule:
        schedule_id = uuid.uuid4()
        now = datetime.now(UTC)
        schedule = ClassSchedule(
            id=schedule_id,
            course_id=course_id,
            day_of_week=day_of_week,
            start_time=start_time,
            end_time=end_time,
            start_date=start_date,
            end_date=end_date,
            location=location.strip() if location else None,
            created_at=now,
            updated_at=now,
        )
        self.schedules[schedule_id] = schedule
        return schedule

    async def create_sessions_batch(
        self,
        sessions_data: list[dict[str, Any]],
    ) -> list[ClassSession]:
        created: list[ClassSession] = []
        now = datetime.now(UTC)
        for item in sessions_data:
            session_id = item.get("id", uuid.uuid4())
            session = ClassSession(
                id=session_id,
                schedule_id=item["schedule_id"],
                starts_at=item["starts_at"],
                ends_at=item["ends_at"],
                location=item.get("location"),
                status=item.get("status", "scheduled"),
                created_at=now,
                updated_at=now,
            )
            self.sessions[session_id] = session
            created.append(session)
        return created

    async def get_schedule_by_id(self, schedule_id: uuid.UUID) -> ClassSchedule | None:
        return self.schedules.get(schedule_id)

    async def get_schedule_by_id_and_user(
        self,
        schedule_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ClassSchedule | None:
        schedule = self.schedules.get(schedule_id)
        if not schedule:
            return None
        course = await self.course_repo.get_by_id_and_user(schedule.course_id, user_id)
        if not course:
            return None
        return schedule

    async def list_schedules_by_user(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[ClassSchedule], int]:
        matched: list[ClassSchedule] = []
        for s in self.schedules.values():
            course = await self.course_repo.get_by_id_and_user(s.course_id, user_id)
            if not course:
                continue
            if course_id is not None and s.course_id != course_id:
                continue
            matched.append(s)

        matched.sort(
            key=lambda x: (
                x.start_date,
                x.day_of_week,
                x.start_time,
            )
        )
        total = len(matched)
        paginated = matched[skip : skip + limit]
        return paginated, total

    async def list_sessions_by_user(
        self,
        user_id: uuid.UUID,
        schedule_id: uuid.UUID | None = None,
        from_date: datetime | date | None = None,
        to_date: datetime | date | None = None,
        status: str | None = None,
        course_id: uuid.UUID | None = None,
    ) -> list[ClassSession]:
        matched: list[ClassSession] = []

        from_dt: datetime | None = None
        if from_date is not None:
            if isinstance(from_date, datetime):
                from_dt = from_date if from_date.tzinfo else from_date.replace(tzinfo=UTC)
            else:
                from_dt = datetime.combine(from_date, time.min, tzinfo=UTC)

        to_dt: datetime | None = None
        if to_date is not None:
            if isinstance(to_date, datetime):
                to_dt = to_date if to_date.tzinfo else to_date.replace(tzinfo=UTC)
            else:
                to_dt = datetime.combine(to_date, time.max, tzinfo=UTC)

        for session in self.sessions.values():
            schedule = self.schedules.get(session.schedule_id)
            if not schedule:
                continue
            course = await self.course_repo.get_by_id_and_user(schedule.course_id, user_id)
            if not course:
                continue

            if schedule_id is not None and session.schedule_id != schedule_id:
                continue
            if from_dt is not None and session.starts_at < from_dt:
                continue
            if to_dt is not None and session.starts_at > to_dt:
                continue
            if status is not None and session.status != status:
                continue

            matched.append(session)

        matched.sort(key=lambda x: x.starts_at)
        return matched

    async def get_session_by_id(self, session_id: uuid.UUID) -> ClassSession | None:
        return self.sessions.get(session_id)

    async def get_session_by_id_and_user(
        self,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ClassSession | None:
        session = self.sessions.get(session_id)
        if not session:
            return None
        schedule = self.schedules.get(session.schedule_id)
        if not schedule:
            return None
        course = await self.course_repo.get_by_id_and_user(schedule.course_id, user_id)
        if not course:
            return None
        return session

    async def update_session(
        self,
        session: ClassSession,
        **kwargs: Any,
    ) -> ClassSession:
        allowed_fields = {"status", "location", "starts_at", "ends_at"}
        for key, value in kwargs.items():
            if key in allowed_fields and value is not None:
                setattr(session, key, value)
        session.updated_at = datetime.now(UTC)
        return session

    async def delete_schedule(self, schedule: ClassSchedule) -> None:
        self.schedules.pop(schedule.id, None)
        # Cascade delete sessions for this schedule
        to_delete = [
            sid for sid, s in self.sessions.items() if s.schedule_id == schedule.id
        ]
        for sid in to_delete:
            self.sessions.pop(sid, None)


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
def fake_schedule_repo(fake_course_repo: FakeCourseRepository) -> FakeScheduleRepository:
    return FakeScheduleRepository(course_repo=fake_course_repo)


@pytest.fixture
def schedule_service(
    fake_schedule_repo: FakeScheduleRepository,
    fake_course_repo: FakeCourseRepository,
) -> ScheduleService:
    return ScheduleService(
        schedule_repo=fake_schedule_repo,
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
    fake_schedule_repo: FakeScheduleRepository,
) -> AsyncGenerator[AsyncClient, None]:
    """Test HTTP client with dependency overrides for repositories and services."""
    service = ScheduleService(
        schedule_repo=fake_schedule_repo,
        course_repo=fake_course_repo,
    )
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_course_repo] = lambda: fake_course_repo
    app.dependency_overrides[get_schedule_repo] = lambda: fake_schedule_repo
    app.dependency_overrides[get_schedule_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def _auth_headers(user: User) -> dict[str, str]:
    """Generate Authorization header with valid Bearer token."""
    token = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Repository Layer Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_create_schedule_and_batch_sessions(
    fake_schedule_repo: FakeScheduleRepository,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Verify repository creation of class schedule and batch session persistence."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Algorithms")
    schedule = await fake_schedule_repo.create_schedule(
        course_id=course.id,
        day_of_week=0,
        start_time=time(9, 0),
        end_time=time(10, 30),
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 16),
        location="Room C201",
    )

    assert schedule.id is not None
    assert schedule.course_id == course.id
    assert schedule.day_of_week == 0
    assert schedule.location == "Room C201"

    sessions = await fake_schedule_repo.create_sessions_batch(
        [
            {
                "schedule_id": schedule.id,
                "starts_at": datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
                "ends_at": datetime(2026, 11, 2, 10, 30, tzinfo=UTC),
                "location": "Room C201",
                "status": "scheduled",
            },
            {
                "schedule_id": schedule.id,
                "starts_at": datetime(2026, 11, 9, 9, 0, tzinfo=UTC),
                "ends_at": datetime(2026, 11, 9, 10, 30, tzinfo=UTC),
                "location": "Room C201",
                "status": "scheduled",
            },
        ]
    )

    assert len(sessions) == 2
    assert sessions[0].status == "scheduled"

    fetched = await fake_schedule_repo.get_schedule_by_id_and_user(schedule.id, student_a.id)
    assert fetched is not None
    assert fetched.id == schedule.id


@pytest.mark.asyncio
async def test_repo_get_schedule_ownership_isolation(
    fake_schedule_repo: FakeScheduleRepository,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Verify get_schedule_by_id_and_user rejects access to unowned schedules."""
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Data Structures")
    schedule_a = await fake_schedule_repo.create_schedule(
        course_id=course_a.id,
        day_of_week=2,
        start_time=time(14, 0),
        end_time=time(16, 0),
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 30),
    )

    # Student A can retrieve
    assert (
        await fake_schedule_repo.get_schedule_by_id_and_user(schedule_a.id, student_a.id)
        is not None
    )
    # Student B cannot retrieve
    assert (
        await fake_schedule_repo.get_schedule_by_id_and_user(schedule_a.id, student_b.id)
        is None
    )


@pytest.mark.asyncio
async def test_repo_list_sessions_filters(
    fake_schedule_repo: FakeScheduleRepository,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Verify list_sessions_by_user filters by schedule_id, date range, and status."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Computer Networks")
    schedule = await fake_schedule_repo.create_schedule(
        course_id=course.id,
        day_of_week=1,
        start_time=time(8, 0),
        end_time=time(10, 0),
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 30),
    )

    await fake_schedule_repo.create_sessions_batch(
        [
            {
                "schedule_id": schedule.id,
                "starts_at": datetime(2026, 11, 3, 8, 0, tzinfo=UTC),
                "ends_at": datetime(2026, 11, 3, 10, 0, tzinfo=UTC),
                "status": "completed",
            },
            {
                "schedule_id": schedule.id,
                "starts_at": datetime(2026, 11, 10, 8, 0, tzinfo=UTC),
                "ends_at": datetime(2026, 11, 10, 10, 0, tzinfo=UTC),
                "status": "scheduled",
            },
            {
                "schedule_id": schedule.id,
                "starts_at": datetime(2026, 11, 17, 8, 0, tzinfo=UTC),
                "ends_at": datetime(2026, 11, 17, 10, 0, tzinfo=UTC),
                "status": "cancelled",
            },
        ]
    )

    # Filter status='cancelled'
    cancelled = await fake_schedule_repo.list_sessions_by_user(
        user_id=student_a.id,
        status="cancelled",
    )
    assert len(cancelled) == 1
    assert cancelled[0].status == "cancelled"

    # Filter date range [2026-11-05, 2026-11-15]
    mid = await fake_schedule_repo.list_sessions_by_user(
        user_id=student_a.id,
        from_date=date(2026, 11, 5),
        to_date=date(2026, 11, 15),
    )
    assert len(mid) == 1
    assert mid[0].starts_at == datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_repo_update_session_and_delete_schedule_cascade(
    fake_schedule_repo: FakeScheduleRepository,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Verify session update and schedule deletion cascading to its sessions."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Operating Systems")
    schedule = await fake_schedule_repo.create_schedule(
        course_id=course.id,
        day_of_week=4,
        start_time=time(13, 0),
        end_time=time(15, 0),
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 15),
    )
    sessions = await fake_schedule_repo.create_sessions_batch(
        [
            {
                "schedule_id": schedule.id,
                "starts_at": datetime(2026, 11, 6, 13, 0, tzinfo=UTC),
                "ends_at": datetime(2026, 11, 6, 15, 0, tzinfo=UTC),
                "status": "scheduled",
            }
        ]
    )
    session = sessions[0]

    # Update session status
    updated = await fake_schedule_repo.update_session(session, status="cancelled")
    assert updated.status == "cancelled"

    # Delete schedule
    await fake_schedule_repo.delete_schedule(schedule)
    assert await fake_schedule_repo.get_schedule_by_id(schedule.id) is None
    assert await fake_schedule_repo.get_session_by_id(session.id) is None


# ---------------------------------------------------------------------------
# Service Layer Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_create_schedule_generates_recurring_sessions(
    schedule_service: ScheduleService,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Verify ScheduleService generates matching class_sessions for all recurring dates."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Software Engineering")

    # November 2026: Nov 2 is Monday (weekday=0), Nov 9 (Mon), Nov 16 (Mon), Nov 23 (Mon), Nov 30 (Mon)
    # Range Nov 2 to Nov 23 has 4 Mondays
    schedule_dto = await schedule_service.create_schedule(
        user_id=student_a.id,
        course_id=course.id,
        day_of_week=0,
        start_time=time(8, 30),
        end_time=time(10, 0),
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 23),
        location="Lab 101",
    )

    assert schedule_dto.id is not None
    assert schedule_dto.day_of_week == 0
    assert schedule_dto.location == "Lab 101"

    sessions = await schedule_service.list_sessions(
        user_id=student_a.id,
        schedule_id=schedule_dto.id,
    )
    assert len(sessions) == 4

    expected_dates = [
        datetime(2026, 11, 2, 8, 30, tzinfo=UTC),
        datetime(2026, 11, 9, 8, 30, tzinfo=UTC),
        datetime(2026, 11, 16, 8, 30, tzinfo=UTC),
        datetime(2026, 11, 23, 8, 30, tzinfo=UTC),
    ]
    for s, exp_start in zip(sessions, expected_dates, strict=True):
        assert s.starts_at == exp_start
        assert s.ends_at == exp_start + timedelta(minutes=90)
        assert s.location == "Lab 101"
        assert s.status == "scheduled"


@pytest.mark.asyncio
async def test_service_create_schedule_ownership_and_validation(
    schedule_service: ScheduleService,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Verify CourseNotFoundError on unowned course and ScheduleValidationError on invalid inputs."""
    course_b = await fake_course_repo.create(user_id=student_b.id, name="Machine Learning")

    # Student A cannot create schedule under Student B's course
    with pytest.raises(CourseNotFoundError):
        await schedule_service.create_schedule(
            user_id=student_a.id,
            course_id=course_b.id,
            day_of_week=1,
            start_time=time(9, 0),
            end_time=time(10, 0),
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 30),
        )

    # Valid course for Student A
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Databases")

    # Invalid day_of_week (must be 0..6)
    with pytest.raises(ScheduleValidationError):
        await schedule_service.create_schedule(
            user_id=student_a.id,
            course_id=course_a.id,
            day_of_week=7,
            start_time=time(9, 0),
            end_time=time(10, 0),
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 30),
        )

    # Invalid end_time <= start_time
    with pytest.raises(ScheduleValidationError):
        await schedule_service.create_schedule(
            user_id=student_a.id,
            course_id=course_a.id,
            day_of_week=0,
            start_time=time(10, 0),
            end_time=time(9, 0),
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 30),
        )

    # Invalid end_date < start_date
    with pytest.raises(ScheduleValidationError):
        await schedule_service.create_schedule(
            user_id=student_a.id,
            course_id=course_a.id,
            day_of_week=0,
            start_time=time(9, 0),
            end_time=time(10, 0),
            start_date=date(2026, 12, 1),
            end_date=date(2026, 11, 1),
        )


@pytest.mark.asyncio
async def test_service_update_session_status_and_not_found(
    schedule_service: ScheduleService,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Verify updating session status and rejecting invalid status or cross-student access."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Compiler Design")
    schedule = await schedule_service.create_schedule(
        user_id=student_a.id,
        course_id=course.id,
        day_of_week=3,
        start_time=time(10, 0),
        end_time=time(11, 30),
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 10),
    )
    sessions = await schedule_service.list_sessions(user_id=student_a.id, schedule_id=schedule.id)
    session = sessions[0]

    # Valid status updates
    updated = await schedule_service.update_session_status(
        session_id=session.id,
        user_id=student_a.id,
        status="cancelled",
    )
    assert updated.status == "cancelled"

    updated2 = await schedule_service.update_session_status(
        session_id=session.id,
        user_id=student_a.id,
        status="completed",
    )
    assert updated2.status == "completed"

    # Invalid status
    with pytest.raises(ScheduleValidationError):
        await schedule_service.update_session_status(
            session_id=session.id,
            user_id=student_a.id,
            status="invalid_status",
        )

    # Student B cannot update Student A's session
    with pytest.raises(SessionNotFoundError):
        await schedule_service.update_session_status(
            session_id=session.id,
            user_id=student_b.id,
            status="cancelled",
        )


# ---------------------------------------------------------------------------
# API Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_create_schedule_201_and_sessions_generated(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Verify POST /api/v1/schedules creates schedule and concrete sessions."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Cloud Computing")

    # Wednesday = day_of_week 2
    # Nov 4, 11, 18, 25 are Wednesdays in Nov 2026
    payload = {
        "course_id": str(course.id),
        "day_of_week": 2,
        "start_time": "14:00:00",
        "end_time": "16:00:00",
        "start_date": "2026-11-01",
        "end_date": "2026-11-30",
        "location": "Room Hall 4B",
    }
    response = await client.post(
        "/api/v1/schedules",
        json=payload,
        headers=_auth_headers(student_a),
    )
    assert response.status_code == 201, response.text
    res_json = response.json()
    assert res_json["success"] is True
    sched_id = res_json["data"]["id"]
    assert res_json["data"]["day_of_week"] == 2
    assert res_json["data"]["location"] == "Room Hall 4B"

    # Verify sessions were generated and can be queried
    sessions_resp = await client.get(
        f"/api/v1/schedules/sessions?schedule_id={sched_id}",
        headers=_auth_headers(student_a),
    )
    assert sessions_resp.status_code == 200
    sessions_json = sessions_resp.json()
    assert sessions_json["success"] is True
    assert sessions_json["data"]["total"] == 4


@pytest.mark.asyncio
async def test_api_create_schedule_under_other_student_course_404(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Verify POST /api/v1/schedules returns 404 when referencing another student's course."""
    course_b = await fake_course_repo.create(user_id=student_b.id, name="Robotics")

    payload = {
        "course_id": str(course_b.id),
        "day_of_week": 1,
        "start_time": "09:00:00",
        "end_time": "10:30:00",
        "start_date": "2026-11-01",
        "end_date": "2026-11-30",
    }
    response = await client.post(
        "/api/v1/schedules",
        json=payload,
        headers=_auth_headers(student_a),
    )
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Course not found."


@pytest.mark.asyncio
async def test_api_create_schedule_validation_422(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Verify POST /api/v1/schedules returns 422 for invalid date, time, or day_of_week inputs."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Physics")

    # 1. end_time <= start_time
    resp1 = await client.post(
        "/api/v1/schedules",
        json={
            "course_id": str(course.id),
            "day_of_week": 0,
            "start_time": "11:00:00",
            "end_time": "10:00:00",
            "start_date": "2026-11-01",
            "end_date": "2026-11-30",
        },
        headers=_auth_headers(student_a),
    )
    assert resp1.status_code == 422

    # 2. end_date < start_date
    resp2 = await client.post(
        "/api/v1/schedules",
        json={
            "course_id": str(course.id),
            "day_of_week": 0,
            "start_time": "09:00:00",
            "end_time": "10:00:00",
            "start_date": "2026-12-01",
            "end_date": "2026-11-01",
        },
        headers=_auth_headers(student_a),
    )
    assert resp2.status_code == 422

    # 3. day_of_week > 6
    resp3 = await client.post(
        "/api/v1/schedules",
        json={
            "course_id": str(course.id),
            "day_of_week": 7,
            "start_time": "09:00:00",
            "end_time": "10:00:00",
            "start_date": "2026-11-01",
            "end_date": "2026-11-30",
        },
        headers=_auth_headers(student_a),
    )
    assert resp3.status_code == 422


@pytest.mark.asyncio
async def test_api_list_schedules_and_sessions_isolation(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Verify GET /api/v1/schedules and /sessions enforce strict student tenant isolation."""
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Course A")
    course_b = await fake_course_repo.create(user_id=student_b.id, name="Course B")

    # Create schedule for Student A
    await client.post(
        "/api/v1/schedules",
        json={
            "course_id": str(course_a.id),
            "day_of_week": 1,
            "start_time": "08:00:00",
            "end_time": "09:30:00",
            "start_date": "2026-11-01",
            "end_date": "2026-11-15",
        },
        headers=_auth_headers(student_a),
    )

    # Create schedule for Student B
    await client.post(
        "/api/v1/schedules",
        json={
            "course_id": str(course_b.id),
            "day_of_week": 4,
            "start_time": "13:00:00",
            "end_time": "15:00:00",
            "start_date": "2026-11-01",
            "end_date": "2026-11-15",
        },
        headers=_auth_headers(student_b),
    )

    # Student A lists schedules
    resp_a = await client.get("/api/v1/schedules", headers=_auth_headers(student_a))
    assert resp_a.status_code == 200
    items_a = resp_a.json()["data"]["items"]
    assert len(items_a) == 1
    assert items_a[0]["course_id"] == str(course_a.id)

    # Student B lists schedules
    resp_b = await client.get("/api/v1/schedules", headers=_auth_headers(student_b))
    assert resp_b.status_code == 200
    items_b = resp_b.json()["data"]["items"]
    assert len(items_b) == 1
    assert items_b[0]["course_id"] == str(course_b.id)

    # Student A lists sessions -> only gets Student A's sessions
    sess_a = await client.get("/api/v1/schedules/sessions", headers=_auth_headers(student_a))
    assert sess_a.status_code == 200
    for item in sess_a.json()["data"]["items"]:
        assert item["schedule_id"] == items_a[0]["id"]


@pytest.mark.asyncio
async def test_api_cross_student_access_rejection_404(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Verify Student B cannot GET or DELETE Student A's schedule or PATCH session."""
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Computer Architecture")
    create_resp = await client.post(
        "/api/v1/schedules",
        json={
            "course_id": str(course_a.id),
            "day_of_week": 3,
            "start_time": "10:00:00",
            "end_time": "11:30:00",
            "start_date": "2026-11-01",
            "end_date": "2026-11-10",
        },
        headers=_auth_headers(student_a),
    )
    sched_id = create_resp.json()["data"]["id"]

    # Student B attempts GET schedule
    get_resp = await client.get(
        f"/api/v1/schedules/{sched_id}",
        headers=_auth_headers(student_b),
    )
    assert get_resp.status_code == 404

    # Student B attempts DELETE schedule
    del_resp = await client.delete(
        f"/api/v1/schedules/{sched_id}",
        headers=_auth_headers(student_b),
    )
    assert del_resp.status_code == 404

    # Get generated session
    sess_resp = await client.get(
        f"/api/v1/schedules/sessions?schedule_id={sched_id}",
        headers=_auth_headers(student_a),
    )
    session_id = sess_resp.json()["data"]["items"][0]["id"]

    # Student B attempts PATCH session status
    patch_resp = await client.patch(
        f"/api/v1/schedules/sessions/{session_id}",
        json={"status": "cancelled"},
        headers=_auth_headers(student_b),
    )
    assert patch_resp.status_code == 404


@pytest.mark.asyncio
async def test_api_update_session_status_and_validation(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Verify PATCH /api/v1/schedules/sessions/{session_id} updates session status."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Discrete Math")
    create_resp = await client.post(
        "/api/v1/schedules",
        json={
            "course_id": str(course.id),
            "day_of_week": 0,
            "start_time": "08:00:00",
            "end_time": "09:30:00",
            "start_date": "2026-11-01",
            "end_date": "2026-11-10",
        },
        headers=_auth_headers(student_a),
    )
    sched_id = create_resp.json()["data"]["id"]

    sess_resp = await client.get(
        f"/api/v1/schedules/sessions?schedule_id={sched_id}",
        headers=_auth_headers(student_a),
    )
    session_id = sess_resp.json()["data"]["items"][0]["id"]

    # Mark as completed
    patch1 = await client.patch(
        f"/api/v1/schedules/sessions/{session_id}",
        json={"status": "completed"},
        headers=_auth_headers(student_a),
    )
    assert patch1.status_code == 200
    assert patch1.json()["data"]["status"] == "completed"

    # Mark as cancelled
    patch2 = await client.patch(
        f"/api/v1/schedules/sessions/{session_id}",
        json={"status": "cancelled"},
        headers=_auth_headers(student_a),
    )
    assert patch2.status_code == 200
    assert patch2.json()["data"]["status"] == "cancelled"

    # Invalid status -> 422
    patch_invalid = await client.patch(
        f"/api/v1/schedules/sessions/{session_id}",
        json={"status": "not_a_valid_status"},
        headers=_auth_headers(student_a),
    )
    assert patch_invalid.status_code == 422


@pytest.mark.asyncio
async def test_api_delete_schedule_cascades_to_sessions(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Verify DELETE /api/v1/schedules/{schedule_id} deletes schedule and cascades sessions."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Web Development")
    create_resp = await client.post(
        "/api/v1/schedules",
        json={
            "course_id": str(course.id),
            "day_of_week": 4,
            "start_time": "14:00:00",
            "end_time": "16:00:00",
            "start_date": "2026-11-01",
            "end_date": "2026-11-15",
        },
        headers=_auth_headers(student_a),
    )
    sched_id = create_resp.json()["data"]["id"]

    # Check sessions exist
    sess_resp = await client.get(
        f"/api/v1/schedules/sessions?schedule_id={sched_id}",
        headers=_auth_headers(student_a),
    )
    assert sess_resp.json()["data"]["total"] > 0

    # Delete schedule
    del_resp = await client.delete(
        f"/api/v1/schedules/{sched_id}",
        headers=_auth_headers(student_a),
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["success"] is True

    # Confirm schedule is gone
    get_resp = await client.get(
        f"/api/v1/schedules/{sched_id}",
        headers=_auth_headers(student_a),
    )
    assert get_resp.status_code == 404

    # Confirm sessions are deleted
    sess_after = await client.get(
        f"/api/v1/schedules/sessions?schedule_id={sched_id}",
        headers=_auth_headers(student_a),
    )
    assert sess_after.status_code == 404


@pytest.mark.asyncio
async def test_api_unauthorized_and_inactive_user(
    client: AsyncClient,
    inactive_student: User,
) -> None:
    """Verify 401 on unauthenticated requests and 403 on deactivated accounts."""
    # 401 Unauthorized
    resp_unauth = await client.get("/api/v1/schedules")
    assert resp_unauth.status_code == 401

    # 403 Forbidden for inactive student
    resp_inactive = await client.get(
        "/api/v1/schedules",
        headers=_auth_headers(inactive_student),
    )
    assert resp_inactive.status_code == 403


@pytest.mark.asyncio
async def test_service_get_schedule_and_delete_not_found(
    schedule_service: ScheduleService,
    student_a: User,
) -> None:
    """Verify ScheduleNotFoundError on non-existent schedule get and delete."""
    random_id = uuid.uuid4()
    with pytest.raises(ScheduleNotFoundError):
        await schedule_service.get_schedule(random_id, student_a.id)

    with pytest.raises(ScheduleNotFoundError):
        await schedule_service.delete_schedule(random_id, student_a.id)


@pytest.mark.asyncio
async def test_service_list_schedules_pagination_and_validation(
    schedule_service: ScheduleService,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Verify list_schedules pagination validation and course filtering."""
    with pytest.raises(ScheduleValidationError):
        await schedule_service.list_schedules(student_a.id, skip=-1)

    with pytest.raises(ScheduleValidationError):
        await schedule_service.list_schedules(student_a.id, limit=0)

    with pytest.raises(ScheduleValidationError):
        await schedule_service.list_schedules(student_a.id, limit=101)

    # Filter with non-existent course_id
    with pytest.raises(CourseNotFoundError):
        await schedule_service.list_schedules(student_a.id, course_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_service_list_sessions_date_range_validation(
    schedule_service: ScheduleService,
    student_a: User,
) -> None:
    """Verify ScheduleValidationError when to_date is earlier than from_date."""
    with pytest.raises(ScheduleValidationError):
        await schedule_service.list_sessions(
            user_id=student_a.id,
            from_date=date(2026, 11, 20),
            to_date=date(2026, 11, 10),
        )


@pytest.mark.asyncio
async def test_api_get_schedule_success_200(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Verify GET /api/v1/schedules/{schedule_id} returns 200 with schedule data."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Artificial Intelligence")
    create_resp = await client.post(
        "/api/v1/schedules",
        json={
            "course_id": str(course.id),
            "day_of_week": 5,
            "start_time": "09:00:00",
            "end_time": "11:00:00",
            "start_date": "2026-11-01",
            "end_date": "2026-11-30",
            "location": "Room 501",
        },
        headers=_auth_headers(student_a),
    )
    sched_id = create_resp.json()["data"]["id"]

    get_resp = await client.get(
        f"/api/v1/schedules/{sched_id}",
        headers=_auth_headers(student_a),
    )
    assert get_resp.status_code == 200
    res = get_resp.json()
    assert res["success"] is True
    assert res["data"]["id"] == sched_id
    assert res["data"]["location"] == "Room 501"


@pytest.mark.asyncio
async def test_api_list_schedules_course_filter_and_pagination(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Verify GET /api/v1/schedules with course_id filter, skip, and limit."""
    course1 = await fake_course_repo.create(user_id=student_a.id, name="Course 1")
    course2 = await fake_course_repo.create(user_id=student_a.id, name="Course 2")

    await client.post(
        "/api/v1/schedules",
        json={
            "course_id": str(course1.id),
            "day_of_week": 0,
            "start_time": "08:00:00",
            "end_time": "09:00:00",
            "start_date": "2026-11-01",
            "end_date": "2026-11-10",
        },
        headers=_auth_headers(student_a),
    )

    await client.post(
        "/api/v1/schedules",
        json={
            "course_id": str(course2.id),
            "day_of_week": 1,
            "start_time": "10:00:00",
            "end_time": "11:00:00",
            "start_date": "2026-11-01",
            "end_date": "2026-11-10",
        },
        headers=_auth_headers(student_a),
    )

    # Filter course1
    resp_filtered = await client.get(
        f"/api/v1/schedules?course_id={course1.id}",
        headers=_auth_headers(student_a),
    )
    assert resp_filtered.status_code == 200
    assert resp_filtered.json()["data"]["total"] == 1
    assert resp_filtered.json()["data"]["items"][0]["course_id"] == str(course1.id)

    # Unowned course filter -> 404
    resp_unowned = await client.get(
        f"/api/v1/schedules?course_id={uuid.uuid4()}",
        headers=_auth_headers(student_a),
    )
    assert resp_unowned.status_code == 404


@pytest.mark.asyncio
async def test_api_list_sessions_invalid_date_format_422(
    client: AsyncClient,
    student_a: User,
) -> None:
    """Verify GET /api/v1/schedules/sessions returns 422 for malformed from_date or to_date."""
    resp1 = await client.get(
        "/api/v1/schedules/sessions?from_date=not-a-date",
        headers=_auth_headers(student_a),
    )
    assert resp1.status_code == 422

    resp2 = await client.get(
        "/api/v1/schedules/sessions?to_date=bad-date",
        headers=_auth_headers(student_a),
    )
    assert resp2.status_code == 422

