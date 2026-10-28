"""Comprehensive tests for Consolidated Academic Calendar Aggregation API and Service (US 2.8)."""

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

from app.api.v1.calendar import router as calendar_router
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
from app.repositories.exam_repo import (
    Exam,
    ExamRepository,
    get_exam_repo,
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
from app.services.calendar_service import (
    CalendarService,
    CalendarValidationError,
    CourseNotFoundError,
    get_calendar_service,
)

# Ensure calendar router is mounted
if not any(
    getattr(route, "path", "").startswith("/api/v1/calendar") for route in app.routes
):
    app.include_router(calendar_router)


# ---------------------------------------------------------------------------
# Test Doubles: In-Memory Fake Repositories
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for authentication in calendar testing."""

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
    """In-memory CourseRepository double for calendar testing."""

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

    async def list_all_by_user(self, user_id: uuid.UUID) -> list[Course]:
        return [c for c in self.courses.values() if c.user_id == user_id]

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        status: str | None = None,
        semester: str | None = None,
        academic_year: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Course], int]:
        matched = [c for c in self.courses.values() if c.user_id == user_id]
        if status:
            matched = [c for c in matched if c.status == status]
        return matched[skip : skip + limit], len(matched)


class FakeAssignmentRepository(AssignmentRepository):
    """In-memory AssignmentRepository double for calendar testing."""

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
        user_courses = {c.id for c in await self.course_repo.list_all_by_user(user_id)}
        matched = [
            a for a in self.assignments.values()
            if a.course_id in user_courses
        ]
        if course_id:
            matched = [a for a in matched if a.course_id == course_id]
        if status:
            matched = [a for a in matched if a.status == status]
        if priority:
            matched = [a for a in matched if a.priority == priority]
        return matched[skip : skip + limit], len(matched)

    async def list_by_date_range(
        self,
        user_id: uuid.UUID,
        from_date: datetime,
        to_date: datetime,
        course_id: uuid.UUID | None = None,
    ) -> list[Assignment]:
        user_courses = {c.id for c in await self.course_repo.list_all_by_user(user_id)}
        from_dt = from_date if from_date.tzinfo else from_date.replace(tzinfo=UTC)
        to_dt = to_date if to_date.tzinfo else to_date.replace(tzinfo=UTC)

        matched: list[Assignment] = []
        for a in self.assignments.values():
            if a.course_id not in user_courses:
                continue
            if course_id is not None and a.course_id != course_id:
                continue
            if a.due_at is None:
                continue
            a_due = a.due_at if a.due_at.tzinfo else a.due_at.replace(tzinfo=UTC)
            if from_dt <= a_due <= to_dt:
                matched.append(a)

        matched.sort(key=lambda a: a.due_at)  # type: ignore[arg-type, return-value]
        return matched


class FakeExamRepository(ExamRepository):
    """In-memory ExamRepository double for calendar testing."""

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
        user_courses = {c.id for c in await self.course_repo.list_all_by_user(user_id)}
        matched = [e for e in self.exams.values() if e.course_id in user_courses]
        if course_id:
            matched = [e for e in matched if e.course_id == course_id]
        if upcoming_only:
            now = datetime.now(UTC)
            matched = [
                e for e in matched
                if (e.exam_at if e.exam_at.tzinfo else e.exam_at.replace(tzinfo=UTC)) >= now
            ]
        return matched[skip : skip + limit], len(matched)

    async def list_by_date_range(
        self,
        user_id: uuid.UUID,
        from_date: datetime,
        to_date: datetime,
        course_id: uuid.UUID | None = None,
    ) -> list[Exam]:
        user_courses = {c.id for c in await self.course_repo.list_all_by_user(user_id)}
        from_dt = from_date if from_date.tzinfo else from_date.replace(tzinfo=UTC)
        to_dt = to_date if to_date.tzinfo else to_date.replace(tzinfo=UTC)

        matched: list[Exam] = []
        for e in self.exams.values():
            if e.course_id not in user_courses:
                continue
            if course_id is not None and e.course_id != course_id:
                continue
            e_at = e.exam_at if e.exam_at.tzinfo else e.exam_at.replace(tzinfo=UTC)
            if from_dt <= e_at <= to_dt:
                matched.append(e)

        matched.sort(key=lambda e: e.exam_at)
        return matched


class FakeScheduleRepository(ScheduleRepository):
    """In-memory ScheduleRepository double for calendar testing."""

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
        for data in sessions_data:
            session_id = data.get("id", uuid.uuid4())
            session = ClassSession(
                id=session_id,
                schedule_id=data["schedule_id"],
                starts_at=data["starts_at"],
                ends_at=data["ends_at"],
                location=data.get("location"),
                status=data.get("status", "scheduled"),
                created_at=now,
                updated_at=now,
            )
            # Associate schedule relationship
            sched = self.schedules.get(session.schedule_id)
            if sched:
                session.schedule = sched
            self.sessions[session_id] = session
            created.append(session)
        return created

    async def get_schedule_by_id(self, schedule_id: uuid.UUID) -> ClassSchedule | None:
        return self.schedules.get(schedule_id)

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
        from_dt = None
        if from_date is not None:
            if isinstance(from_date, datetime):
                from_dt = from_date if from_date.tzinfo else from_date.replace(tzinfo=UTC)
            else:
                from_dt = datetime.combine(from_date, time.min, tzinfo=UTC)

        to_dt = None
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
            if course_id is not None and schedule.course_id != course_id:
                continue
            if schedule_id is not None and session.schedule_id != schedule_id:
                continue
            if status is not None and session.status != status:
                continue

            s_start = session.starts_at if session.starts_at.tzinfo else session.starts_at.replace(tzinfo=UTC)
            if from_dt is not None and s_start < from_dt:
                continue
            if to_dt is not None and s_start > to_dt:
                continue

            # Ensure schedule is linked
            session.schedule = schedule
            matched.append(session)

        matched.sort(key=lambda s: s.starts_at)
        return matched

    async def list_sessions_by_date_range(
        self,
        user_id: uuid.UUID,
        from_date: datetime,
        to_date: datetime,
        course_id: uuid.UUID | None = None,
    ) -> list[ClassSession]:
        return await self.list_sessions_by_user(
            user_id=user_id,
            from_date=from_date,
            to_date=to_date,
            course_id=course_id,
        )


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
def fake_exam_repo(fake_course_repo: FakeCourseRepository) -> FakeExamRepository:
    return FakeExamRepository(course_repo=fake_course_repo)


@pytest.fixture
def fake_schedule_repo(fake_course_repo: FakeCourseRepository) -> FakeScheduleRepository:
    return FakeScheduleRepository(course_repo=fake_course_repo)


@pytest.fixture
def calendar_service(
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_exam_repo: FakeExamRepository,
    fake_schedule_repo: FakeScheduleRepository,
) -> CalendarService:
    return CalendarService(
        course_repo=fake_course_repo,
        assignment_repo=fake_assignment_repo,
        exam_repo=fake_exam_repo,
        schedule_repo=fake_schedule_repo,
    )


@pytest_asyncio.fixture
async def student_a(fake_user_repo: FakeUserRepository) -> User:
    """Primary authenticated student."""
    return await fake_user_repo.create_user(
        email="alice.calendar@university.edu",
        password_hash=hash_password("ValidPass123!"),
        full_name="Alice Student",
    )


@pytest_asyncio.fixture
async def student_b(fake_user_repo: FakeUserRepository) -> User:
    """Secondary student for isolation verification."""
    return await fake_user_repo.create_user(
        email="bob.calendar@university.edu",
        password_hash=hash_password("ValidPass123!"),
        full_name="Bob Student",
    )


@pytest_asyncio.fixture
async def inactive_student(fake_user_repo: FakeUserRepository) -> User:
    """Inactive student for 403 checks."""
    user = await fake_user_repo.create_user(
        email="inactive.calendar@university.edu",
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
    fake_exam_repo: FakeExamRepository,
    fake_schedule_repo: FakeScheduleRepository,
    calendar_service: CalendarService,
) -> AsyncGenerator[AsyncClient, None]:
    """Test HTTP client with dependency overrides."""
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_course_repo] = lambda: fake_course_repo
    app.dependency_overrides[get_assignment_repo] = lambda: fake_assignment_repo
    app.dependency_overrides[get_exam_repo] = lambda: fake_exam_repo
    app.dependency_overrides[get_schedule_repo] = lambda: fake_schedule_repo
    app.dependency_overrides[get_calendar_service] = lambda: calendar_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def _auth_headers(user: User) -> dict[str, str]:
    token = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Service Layer Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_full_aggregation_chronological_order(
    calendar_service: CalendarService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_exam_repo: FakeExamRepository,
    fake_schedule_repo: FakeScheduleRepository,
    student_a: User,
) -> None:
    """Verify aggregation combines class sessions, assignments, and exams in chronological order."""
    # 1. Create course
    course = await fake_course_repo.create(
        user_id=student_a.id,
        name="Data Structures and Algorithms",
        course_code="CS201",
    )

    # Base dates: October 2026
    start_window = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    end_window = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)

    # 2. Create Schedule & Class Sessions (e.g. Session on Oct 10 at 09:00)
    schedule = await fake_schedule_repo.create_schedule(
        course_id=course.id,
        day_of_week=5,  # Saturday
        start_time=time(9, 0),
        end_time=time(11, 0),
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
        location="Room 302",
    )
    session_dt = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    await fake_schedule_repo.create_sessions_batch([
        {
            "schedule_id": schedule.id,
            "starts_at": session_dt,
            "ends_at": session_dt + timedelta(hours=2),
            "location": "Room 302",
            "status": "scheduled",
        }
    ])

    # 3. Create Assignment (e.g. Due on Oct 15 at 23:59)
    assignment_dt = datetime(2026, 10, 15, 23, 59, tzinfo=UTC)
    assignment = await fake_assignment_repo.create(
        course_id=course.id,
        title="Binary Tree Implementation",
        description="Implement AVL tree in Python",
        due_at=assignment_dt,
        priority="high",
        status="pending",
    )

    # 4. Create Exam (e.g. Exam on Oct 5 at 14:00)
    exam_dt = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    exam = await fake_exam_repo.create(
        course_id=course.id,
        title="Midterm Exam",
        exam_at=exam_dt,
        duration_minutes=90,
        location="Hall A",
        notes="Closed book exam",
    )

    # Execute service aggregation
    result = await calendar_service.get_aggregated_calendar(
        user_id=student_a.id,
        from_date=start_window,
        to_date=end_window,
    )

    assert result.total_events == 3
    assert len(result.events) == 3

    # Check chronological ordering:
    # 1st: Exam (Oct 5)
    # 2nd: Session (Oct 10)
    # 3rd: Assignment (Oct 15)
    e1, e2, e3 = result.events

    # Event 1: Exam
    assert e1.event_type == "exam"
    assert e1.id == exam.id
    assert e1.title == "Midterm Exam"
    assert e1.course_name == "Data Structures and Algorithms"
    assert e1.course_code == "CS201"
    assert e1.start_time == exam_dt
    assert e1.end_time == exam_dt + timedelta(minutes=90)
    assert e1.location == "Hall A"
    assert e1.description == "Closed book exam"
    assert e1.status is None
    assert e1.priority is None

    # Event 2: Class Session
    assert e2.event_type == "class_session"
    assert e2.title == "Data Structures and Algorithms"
    assert e2.course_name == "Data Structures and Algorithms"
    assert e2.course_code == "CS201"
    assert e2.start_time == session_dt
    assert e2.end_time == session_dt + timedelta(hours=2)
    assert e2.location == "Room 302"
    assert e2.status == "scheduled"
    assert e2.priority is None
    assert e2.description is None

    # Event 3: Assignment
    assert e3.event_type == "assignment"
    assert e3.id == assignment.id
    assert e3.title == "Binary Tree Implementation"
    assert e3.course_name == "Data Structures and Algorithms"
    assert e3.course_code == "CS201"
    assert e3.start_time == assignment_dt
    assert e3.end_time is None
    assert e3.location is None
    assert e3.status == "pending"
    assert e3.priority == "high"
    assert e3.description == "Implement AVL tree in Python"


@pytest.mark.asyncio
async def test_service_date_range_filtering(
    calendar_service: CalendarService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    student_a: User,
) -> None:
    """Verify events outside of [from_date, to_date] are excluded."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Physics I")

    # Outside: before window
    await fake_assignment_repo.create(
        course_id=course.id,
        title="Past Assignment",
        due_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
    )
    # Inside window
    inside_assignment = await fake_assignment_repo.create(
        course_id=course.id,
        title="Current Assignment",
        due_at=datetime(2026, 10, 15, 10, 0, tzinfo=UTC),
    )
    # Outside: after window
    await fake_assignment_repo.create(
        course_id=course.id,
        title="Future Assignment",
        due_at=datetime(2026, 11, 5, 10, 0, tzinfo=UTC),
    )

    result = await calendar_service.get_aggregated_calendar(
        user_id=student_a.id,
        from_date=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        to_date=datetime(2026, 10, 31, 23, 59, tzinfo=UTC),
    )

    assert result.total_events == 1
    assert result.events[0].id == inside_assignment.id
    assert result.events[0].title == "Current Assignment"


@pytest.mark.asyncio
async def test_service_event_type_filtering(
    calendar_service: CalendarService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_exam_repo: FakeExamRepository,
    fake_schedule_repo: FakeScheduleRepository,
    student_a: User,
) -> None:
    """Verify event_types filter returns only the requested types."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Chemistry")

    # Session
    schedule = await fake_schedule_repo.create_schedule(
        course_id=course.id,
        day_of_week=0,
        start_time=time(10, 0),
        end_time=time(11, 0),
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
    )
    await fake_schedule_repo.create_sessions_batch([
        {
            "schedule_id": schedule.id,
            "starts_at": datetime(2026, 10, 5, 10, 0, tzinfo=UTC),
            "ends_at": datetime(2026, 10, 5, 11, 0, tzinfo=UTC),
        }
    ])

    # Assignment
    await fake_assignment_repo.create(
        course_id=course.id,
        title="Lab Report",
        due_at=datetime(2026, 10, 8, 12, 0, tzinfo=UTC),
    )

    # Exam
    await fake_exam_repo.create(
        course_id=course.id,
        title="Midterm",
        exam_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )

    # Request only assignment and exam (exclude class_session)
    result = await calendar_service.get_aggregated_calendar(
        user_id=student_a.id,
        from_date=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        to_date=datetime(2026, 10, 31, 23, 59, tzinfo=UTC),
        event_types=["assignment", "exam"],
    )

    assert result.total_events == 2
    types = [e.event_type for e in result.events]
    assert "class_session" not in types
    assert set(types) == {"assignment", "exam"}


@pytest.mark.asyncio
async def test_service_course_filter_and_unowned_course(
    calendar_service: CalendarService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Verify course_id filter isolates events to that course and rejects unowned courses."""
    course1 = await fake_course_repo.create(user_id=student_a.id, name="Math")
    course2 = await fake_course_repo.create(user_id=student_a.id, name="History")
    course_b = await fake_course_repo.create(user_id=student_b.id, name="Biology")

    await fake_assignment_repo.create(
        course_id=course1.id,
        title="Math Homework",
        due_at=datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
    )
    await fake_assignment_repo.create(
        course_id=course2.id,
        title="History Essay",
        due_at=datetime(2026, 10, 12, 12, 0, tzinfo=UTC),
    )

    # Query course 1 only
    res = await calendar_service.get_aggregated_calendar(
        user_id=student_a.id,
        from_date=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        to_date=datetime(2026, 10, 31, 23, 59, tzinfo=UTC),
        course_id=course1.id,
    )
    assert res.total_events == 1
    assert res.events[0].title == "Math Homework"

    # Querying course belonging to Student B raises CourseNotFoundError
    with pytest.raises(CourseNotFoundError):
        await calendar_service.get_aggregated_calendar(
            user_id=student_a.id,
            from_date=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
            to_date=datetime(2026, 10, 31, 23, 59, tzinfo=UTC),
            course_id=course_b.id,
        )


@pytest.mark.asyncio
async def test_service_validation_to_date_earlier_than_from_date(
    calendar_service: CalendarService,
    student_a: User,
) -> None:
    """Verify validation error when to_date < from_date."""
    with pytest.raises(CalendarValidationError, match="to_date cannot be earlier than from_date"):
        await calendar_service.get_aggregated_calendar(
            user_id=student_a.id,
            from_date=datetime(2026, 10, 20, 0, 0, tzinfo=UTC),
            to_date=datetime(2026, 10, 10, 0, 0, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_service_validation_invalid_event_type(
    calendar_service: CalendarService,
    student_a: User,
) -> None:
    """Verify validation error when unsupported event_type is supplied."""
    with pytest.raises(CalendarValidationError, match="Invalid event_type"):
        await calendar_service.get_aggregated_calendar(
            user_id=student_a.id,
            from_date=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
            to_date=datetime(2026, 10, 31, 0, 0, tzinfo=UTC),
            event_types=["homework"],
        )


@pytest.mark.asyncio
async def test_service_tenant_isolation(
    calendar_service: CalendarService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_exam_repo: FakeExamRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Verify Student B never sees Student A's events (Invariant 6)."""
    # Create events for Student A
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Student A Course")
    await fake_assignment_repo.create(
        course_id=course_a.id,
        title="Student A Assignment",
        due_at=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
    )
    await fake_exam_repo.create(
        course_id=course_a.id,
        title="Student A Exam",
        exam_at=datetime(2026, 10, 18, 9, 0, tzinfo=UTC),
    )

    # Student B queries calendar
    res_b = await calendar_service.get_aggregated_calendar(
        user_id=student_b.id,
        from_date=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        to_date=datetime(2026, 10, 31, 23, 59, tzinfo=UTC),
    )
    assert res_b.total_events == 0
    assert len(res_b.events) == 0


@pytest.mark.asyncio
async def test_service_assignment_without_due_at_excluded(
    calendar_service: CalendarService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    student_a: User,
) -> None:
    """Verify assignments with due_at=None are omitted from calendar."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Literature")
    await fake_assignment_repo.create(
        course_id=course.id,
        title="Ongoing Reading Task",
        due_at=None,
    )

    res = await calendar_service.get_aggregated_calendar(
        user_id=student_a.id,
        from_date=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        to_date=datetime(2026, 10, 31, 23, 59, tzinfo=UTC),
    )
    assert res.total_events == 0


# ---------------------------------------------------------------------------
# API Layer Tests (HTTP Integration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_calendar_success_all_event_types(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_exam_repo: FakeExamRepository,
    fake_schedule_repo: FakeScheduleRepository,
    student_a: User,
) -> None:
    """Verify GET /api/v1/calendar returns 200 with standard envelope and chronologically ordered events."""
    course = await fake_course_repo.create(
        user_id=student_a.id,
        name="Operating Systems",
        course_code="CS301",
    )

    # 1. Exam: Oct 10 at 10:00
    exam_dt = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
    await fake_exam_repo.create(
        course_id=course.id,
        title="Midterm Exam",
        exam_at=exam_dt,
        duration_minutes=120,
        location="Hall 101",
    )

    # 2. Assignment: Oct 12 at 23:59
    assign_dt = datetime(2026, 10, 12, 23, 59, tzinfo=UTC)
    await fake_assignment_repo.create(
        course_id=course.id,
        title="Kernel Patch Submission",
        due_at=assign_dt,
        priority="high",
        status="pending",
    )

    # 3. Class Session: Oct 5 at 08:00
    schedule = await fake_schedule_repo.create_schedule(
        course_id=course.id,
        day_of_week=0,
        start_time=time(8, 0),
        end_time=time(10, 0),
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
        location="Lab 4",
    )
    session_dt = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    await fake_schedule_repo.create_sessions_batch([
        {
            "schedule_id": schedule.id,
            "starts_at": session_dt,
            "ends_at": session_dt + timedelta(hours=2),
            "location": "Lab 4",
            "status": "scheduled",
        }
    ])

    headers = _auth_headers(student_a)
    response = await client.get(
        "/api/v1/calendar?from_date=2026-10-01T00:00:00Z&to_date=2026-10-31T23:59:59Z",
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["message"] == "Calendar events retrieved successfully."

    data = body["data"]
    assert data["total_events"] == 3
    events = data["events"]
    assert len(events) == 3

    # Check order: Session (Oct 5) -> Exam (Oct 10) -> Assignment (Oct 12)
    assert events[0]["event_type"] == "class_session"
    assert events[0]["title"] == "Operating Systems"
    assert events[0]["course_name"] == "Operating Systems"
    assert events[0]["course_code"] == "CS301"

    assert events[1]["event_type"] == "exam"
    assert events[1]["title"] == "Midterm Exam"
    assert events[1]["location"] == "Hall 101"

    assert events[2]["event_type"] == "assignment"
    assert events[2]["title"] == "Kernel Patch Submission"
    assert events[2]["priority"] == "high"


@pytest.mark.asyncio
async def test_api_calendar_event_types_filter(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_exam_repo: FakeExamRepository,
    student_a: User,
) -> None:
    """Verify event_types query parameter filters out unrequested event types."""
    course = await fake_course_repo.create(user_id=student_a.id, name="Databases")
    await fake_assignment_repo.create(
        course_id=course.id,
        title="SQL Project",
        due_at=datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
    )
    await fake_exam_repo.create(
        course_id=course.id,
        title="Final Exam",
        exam_at=datetime(2026, 10, 25, 9, 0, tzinfo=UTC),
    )

    headers = _auth_headers(student_a)
    # Query with event_types="exam"
    response = await client.get(
        "/api/v1/calendar?from_date=2026-10-01T00:00:00Z&to_date=2026-10-31T23:59:59Z&event_types=exam",
        headers=headers,
    )

    assert response.status_code == 200
    events = response.json()["data"]["events"]
    assert len(events) == 1
    assert events[0]["event_type"] == "exam"
    assert events[0]["title"] == "Final Exam"


@pytest.mark.asyncio
async def test_api_calendar_course_id_filter(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    student_a: User,
) -> None:
    """Verify course_id query parameter filters events to specific course."""
    c1 = await fake_course_repo.create(user_id=student_a.id, name="Course 1")
    c2 = await fake_course_repo.create(user_id=student_a.id, name="Course 2")

    await fake_assignment_repo.create(
        course_id=c1.id,
        title="A1",
        due_at=datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
    )
    await fake_assignment_repo.create(
        course_id=c2.id,
        title="A2",
        due_at=datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
    )

    headers = _auth_headers(student_a)
    response = await client.get(
        f"/api/v1/calendar?from_date=2026-10-01T00:00:00Z&to_date=2026-10-31T23:59:59Z&course_id={c1.id}",
        headers=headers,
    )

    assert response.status_code == 200
    events = response.json()["data"]["events"]
    assert len(events) == 1
    assert events[0]["title"] == "A1"


@pytest.mark.asyncio
async def test_api_calendar_unowned_course_id_404(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Verify querying an unowned course_id returns 404."""
    course_b = await fake_course_repo.create(user_id=student_b.id, name="Bob's Course")

    headers = _auth_headers(student_a)
    response = await client.get(
        f"/api/v1/calendar?from_date=2026-10-01T00:00:00Z&to_date=2026-10-31T23:59:59Z&course_id={course_b.id}",
        headers=headers,
    )

    assert response.status_code == 404
    assert response.json()["success"] is False


@pytest.mark.asyncio
async def test_api_calendar_date_validation_422(
    client: AsyncClient,
    student_a: User,
) -> None:
    """Verify to_date < from_date returns 422."""
    headers = _auth_headers(student_a)
    response = await client.get(
        "/api/v1/calendar?from_date=2026-10-31T00:00:00Z&to_date=2026-10-01T00:00:00Z",
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["success"] is False


@pytest.mark.asyncio
async def test_api_calendar_invalid_event_type_422(
    client: AsyncClient,
    student_a: User,
) -> None:
    """Verify invalid event_types string returns 422."""
    headers = _auth_headers(student_a)
    response = await client.get(
        "/api/v1/calendar?from_date=2026-10-01T00:00:00Z&to_date=2026-10-31T23:59:59Z&event_types=invalid_type",
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["success"] is False


@pytest.mark.asyncio
async def test_api_calendar_tenant_isolation(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Verify Student B querying calendar sees 0 events from Student A."""
    course_a = await fake_course_repo.create(user_id=student_a.id, name="Secret AI Course")
    await fake_assignment_repo.create(
        course_id=course_a.id,
        title="Top Secret Homework",
        due_at=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
    )

    # Bob queries
    headers = _auth_headers(student_b)
    response = await client.get(
        "/api/v1/calendar?from_date=2026-10-01T00:00:00Z&to_date=2026-10-31T23:59:59Z",
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total_events"] == 0
    assert len(data["events"]) == 0


@pytest.mark.asyncio
async def test_api_calendar_default_dates(
    client: AsyncClient,
    student_a: User,
) -> None:
    """Verify calendar endpoint works with default dates when none are provided."""
    headers = _auth_headers(student_a)
    response = await client.get("/api/v1/calendar", headers=headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert "from_date" in data
    assert "to_date" in data
    assert data["total_events"] == 0


@pytest.mark.asyncio
async def test_api_calendar_unauthenticated_401(
    client: AsyncClient,
) -> None:
    """Verify unauthenticated request returns 401."""
    response = await client.get("/api/v1/calendar")
    assert response.status_code == 401
    assert response.json()["success"] is False


@pytest.mark.asyncio
async def test_api_calendar_inactive_user_403(
    client: AsyncClient,
    inactive_student: User,
) -> None:
    """Verify deactivated user request returns 403."""
    headers = _auth_headers(inactive_student)
    response = await client.get("/api/v1/calendar", headers=headers)
    assert response.status_code == 403
    assert response.json()["success"] is False
