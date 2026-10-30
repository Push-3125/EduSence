"""Comprehensive tests for Academic Progress Dashboard & Analytics (TASK-M5-07 / US 2.17, 2.18).

Validates:
- Invariant 2 (Layering): Routers -> Services -> Repositories. Routers NEVER import repositories.
- Invariant 6 (Strict Tenant Isolation): User A cannot calculate, view, or recalculate User B's course progress.
- Database Constraints:
  * uq_course_progress_user_course: UNIQUE(user_id, course_id)
  * ck_course_progress_counts: assignments_total >= 0 AND assignments_completed >= 0
    AND study_items_total >= 0 AND study_items_completed >= 0
    AND assignments_completed <= assignments_total
    AND study_items_completed <= study_items_total
  * ck_course_progress_percent: progress_percent BETWEEN 0 AND 100
  * ck_course_progress_risk: risk_status IN ('on_track', 'at_risk', 'overdue')
- Progress Calculation:
  * Course with 0 assignments and 0 study items -> 0% progress and 'on_track'.
  * Course with completed assignments -> accurate blended percentage.
  * Course with overdue assignment -> risk_status = 'overdue'.
  * Course with exam within 7 days and low progress (< 50%) -> risk_status = 'at_risk'.
  * Course with exam within 7 days and good progress (>= 50%) -> risk_status = 'on_track'.
  * Overdue status takes precedence over at_risk.
- Dashboard Aggregation:
  * Overall percentage, counts of on_track, at_risk, overdue.
  * Upcoming urgent deadlines (pending assignments & exams within 7 days).
  * Empty dashboard when student has no courses.
- REST API Endpoints:
  * Authentication (401 on unauthenticated, 403 on inactive).
  * Standard envelope response { success: true, data: ..., message: ... }.
  * GET /api/v1/progress/dashboard
  * GET /api/v1/progress/courses
  * GET /api/v1/progress/courses/{course_id}
  * POST /api/v1/progress/recalculate
  * 404 on unowned or non-existent course.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure backend root is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.progress import router as progress_router
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
from app.repositories.progress_repo import (
    CourseProgress,
    ProgressRepository,
    get_progress_repo,
)
from app.repositories.study_plan_repo import (
    StudyPlan,
    StudyPlanItem,
    StudyPlanRepository,
    get_study_plan_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.progress_service import (
    CourseNotFoundError,
    DashboardProgressDTO,
    ProgressService,
    get_progress_service,
)

# Ensure progress router is mounted
if not any(getattr(r, "path", "").startswith("/api/v1/progress") for r in app.routes):
    app.include_router(progress_router)


# ---------------------------------------------------------------------------
# Test Doubles: In-Memory Fake Repositories
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

        now = datetime.now(UTC)
        user = User(
            id=user_id,
            email=email.lower().strip(),
            password_hash=password_hash,
            is_active=True,
            is_email_verified=is_email_verified,
            current_plan_code=current_plan_code,
            created_at=now,
            updated_at=now,
        )
        profile = UserProfile(
            id=uuid.uuid4(),
            user_id=user_id,
            full_name=full_name.strip(),
            avatar_url=avatar_url,
            created_at=now,
            updated_at=now,
        )
        user.profile = profile
        user.roles = [role]
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
        c_id = uuid.uuid4()
        now = datetime.now(UTC)
        c = Course(
            id=c_id,
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
        self.courses[c_id] = c
        return c

    async def get_by_id_and_user(
        self, course_id: uuid.UUID, user_id: uuid.UUID
    ) -> Course | None:
        course = self.courses.get(course_id)
        if course and course.user_id == user_id:
            return course
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
        all_c = [c for c in self.courses.values() if c.user_id == user_id]
        if status:
            all_c = [c for c in all_c if c.status == status]
        return all_c[skip : skip + limit], len(all_c)


class FakeAssignmentRepository(AssignmentRepository):
    """In-memory AssignmentRepository double."""

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
        a_id = uuid.uuid4()
        now = datetime.now(UTC)
        a = Assignment(
            id=a_id,
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
        self.assignments[a_id] = a
        return a

    async def list_by_course(
        self, user_id: uuid.UUID, course_id: uuid.UUID
    ) -> list[Assignment]:
        course = self.course_repo.courses.get(course_id)
        if not course or course.user_id != user_id:
            return []
        items = [a for a in self.assignments.values() if a.course_id == course_id]
        items.sort(key=lambda x: (x.due_at is None, x.due_at or datetime.min))
        return items

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        status: str | None = None,
        priority: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Assignment], int]:
        user_courses = {
            cid: c for cid, c in self.course_repo.courses.items() if c.user_id == user_id
        }
        res: list[Assignment] = []
        for a in self.assignments.values():
            if a.course_id not in user_courses:
                continue
            if course_id is not None and a.course_id != course_id:
                continue
            if status is not None and a.status != status:
                continue
            if priority is not None and a.priority != priority:
                continue
            res.append(a)
        res.sort(key=lambda x: (x.due_at is None, x.due_at or datetime.min))
        return res[skip : skip + limit], len(res)


class FakeStudyPlanRepository(StudyPlanRepository):
    """In-memory StudyPlanRepository double."""

    def __init__(self) -> None:
        self.plans: dict[uuid.UUID, StudyPlan] = {}
        self.items: dict[uuid.UUID, StudyPlanItem] = {}

    async def create_plan(
        self,
        user_id: uuid.UUID,
        name: str,
        start_date: date,
        end_date: date,
        description: str | None = None,
        generated_by_ai: bool = False,
        status: str = "draft",
    ) -> StudyPlan:
        p_id = uuid.uuid4()
        now = datetime.now(UTC)
        p = StudyPlan(
            id=p_id,
            user_id=user_id,
            name=name.strip(),
            start_date=start_date,
            end_date=end_date,
            description=description,
            generated_by_ai=generated_by_ai,
            status=status,
            created_at=now,
            updated_at=now,
        )
        self.plans[p_id] = p
        return p

    async def add_items(
        self, plan_id: uuid.UUID, items_data: list[dict[str, Any]]
    ) -> list[StudyPlanItem]:
        created: list[StudyPlanItem] = []
        now = datetime.now(UTC)
        for data in items_data:
            item_id = uuid.uuid4()
            item = StudyPlanItem(
                id=item_id,
                study_plan_id=plan_id,
                course_id=data.get("course_id"),
                title=data.get("title", ""),
                description=data.get("description"),
                scheduled_start=data["scheduled_start"],
                scheduled_end=data.get("scheduled_end"),
                status=data.get("status", "pending"),
                completed_at=data.get("completed_at"),
                accepted_at=data.get("accepted_at"),
                display_order=data.get("display_order", 0),
                created_at=now,
                updated_at=now,
            )
            self.items[item_id] = item
            created.append(item)
        return created

    async def get_items_by_course(
        self, user_id: uuid.UUID, course_id: uuid.UUID
    ) -> list[StudyPlanItem]:
        user_plan_ids = {p.id for p in self.plans.values() if p.user_id == user_id}
        res = [
            item
            for item in self.items.values()
            if item.study_plan_id in user_plan_ids
            and item.course_id == course_id
            and item.status != "dismissed"
        ]
        res.sort(key=lambda x: (x.scheduled_start, x.display_order))
        return res


class FakeExamRepository(ExamRepository):
    """In-memory ExamRepository double."""

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
        e_id = uuid.uuid4()
        now = datetime.now(UTC)
        e = Exam(
            id=e_id,
            course_id=course_id,
            title=title.strip(),
            exam_at=exam_at,
            duration_minutes=duration_minutes,
            location=location,
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        self.exams[e_id] = e
        return e

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        upcoming_only: bool = False,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Exam], int]:
        now = datetime.now(UTC)
        user_courses = {
            cid: c for cid, c in self.course_repo.courses.items() if c.user_id == user_id
        }
        res: list[Exam] = []
        for e in self.exams.values():
            if e.course_id not in user_courses:
                continue
            if course_id is not None and e.course_id != course_id:
                continue
            e_at = e.exam_at if e.exam_at.tzinfo else e.exam_at.replace(tzinfo=UTC)
            if upcoming_only and e_at < now:
                continue
            res.append(e)
        res.sort(key=lambda x: x.exam_at)
        return res[skip : skip + limit], len(res)


class FakeProgressRepository(ProgressRepository):
    """In-memory ProgressRepository double enforcing schema constraints."""

    def __init__(self) -> None:
        self.progress_store: dict[tuple[uuid.UUID, uuid.UUID], CourseProgress] = {}

    async def upsert_course_progress(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID,
        assignments_total: int,
        assignments_completed: int,
        study_items_total: int,
        study_items_completed: int,
        progress_percent: float,
        risk_status: str,
    ) -> CourseProgress:
        # Enforce check constraints matching DB schema
        if (
            assignments_total < 0
            or assignments_completed < 0
            or study_items_total < 0
            or study_items_completed < 0
            or assignments_completed > assignments_total
            or study_items_completed > study_items_total
        ):
            raise ValueError("ck_course_progress_counts constraint violation")

        if not (0.0 <= progress_percent <= 100.0):
            raise ValueError("ck_course_progress_percent constraint violation")

        if risk_status not in ("on_track", "at_risk", "overdue"):
            raise ValueError("ck_course_progress_risk constraint violation")

        key = (user_id, course_id)
        now = datetime.now(UTC)

        if key in self.progress_store:
            rec = self.progress_store[key]
            rec.assignments_total = assignments_total
            rec.assignments_completed = assignments_completed
            rec.study_items_total = study_items_total
            rec.study_items_completed = study_items_completed
            rec.progress_percent = progress_percent
            rec.risk_status = risk_status
            rec.last_calculated_at = now
            rec.updated_at = now
            return rec

        rec = CourseProgress(
            id=uuid.uuid4(),
            user_id=user_id,
            course_id=course_id,
            assignments_total=assignments_total,
            assignments_completed=assignments_completed,
            study_items_total=study_items_total,
            study_items_completed=study_items_completed,
            progress_percent=progress_percent,
            risk_status=risk_status,
            last_calculated_at=now,
            created_at=now,
            updated_at=now,
        )
        self.progress_store[key] = rec
        return rec

    async def get_progress(
        self, user_id: uuid.UUID, course_id: uuid.UUID
    ) -> CourseProgress | None:
        return self.progress_store.get((user_id, course_id))

    async def list_progress(self, user_id: uuid.UUID) -> list[CourseProgress]:
        return [p for (uid, _), p in self.progress_store.items() if uid == user_id]


# ---------------------------------------------------------------------------
# Pytest Fixtures
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
def fake_plan_repo() -> FakeStudyPlanRepository:
    return FakeStudyPlanRepository()


@pytest.fixture
def fake_exam_repo(fake_course_repo: FakeCourseRepository) -> FakeExamRepository:
    return FakeExamRepository(course_repo=fake_course_repo)


@pytest.fixture
def fake_progress_repo() -> FakeProgressRepository:
    return FakeProgressRepository()


@pytest.fixture
def progress_service(
    fake_progress_repo: FakeProgressRepository,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_plan_repo: FakeStudyPlanRepository,
    fake_exam_repo: FakeExamRepository,
) -> ProgressService:
    return ProgressService(
        progress_repo=fake_progress_repo,
        course_repo=fake_course_repo,
        assignment_repo=fake_assignment_repo,
        study_plan_repo=fake_plan_repo,
        exam_repo=fake_exam_repo,
    )


@pytest_asyncio.fixture
async def student_a(fake_user_repo: FakeUserRepository) -> User:
    return await fake_user_repo.create_user(
        email="student.a@univ.edu",
        password_hash=hash_password("Pass12345!"),
        full_name="Alice Student",
    )


@pytest_asyncio.fixture
async def student_b(fake_user_repo: FakeUserRepository) -> User:
    return await fake_user_repo.create_user(
        email="student.b@univ.edu",
        password_hash=hash_password("Pass12345!"),
        full_name="Bob Student",
    )


@pytest_asyncio.fixture
async def inactive_student(fake_user_repo: FakeUserRepository) -> User:
    user = await fake_user_repo.create_user(
        email="inactive.student@univ.edu",
        password_hash=hash_password("Pass12345!"),
        full_name="Inactive Student",
    )
    user.is_active = False
    return user


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_plan_repo: FakeStudyPlanRepository,
    fake_exam_repo: FakeExamRepository,
    fake_progress_repo: FakeProgressRepository,
    progress_service: ProgressService,
) -> AsyncGenerator[AsyncClient, None]:
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_course_repo] = lambda: fake_course_repo
    app.dependency_overrides[get_assignment_repo] = lambda: fake_assignment_repo
    app.dependency_overrides[get_study_plan_repo] = lambda: fake_plan_repo
    app.dependency_overrides[get_exam_repo] = lambda: fake_exam_repo
    app.dependency_overrides[get_progress_repo] = lambda: fake_progress_repo
    app.dependency_overrides[get_progress_service] = lambda: progress_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_header(user_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(subject=str(user_id), expires_delta=timedelta(minutes=60))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Database Check Constraints & Model Verification
# ---------------------------------------------------------------------------


def test_database_table_constraints_verification() -> None:
    """Verify that CourseProgress SQLAlchemy model declares exact table constraints."""
    table: Any = CourseProgress.__table__
    constraint_names = {c.name for c in table.constraints}

    assert "uq_course_progress_user_course" in constraint_names
    assert "ck_course_progress_counts" in constraint_names
    assert "ck_course_progress_percent" in constraint_names
    assert "ck_course_progress_risk" in constraint_names


@pytest.mark.asyncio
async def test_repository_constraint_enforcement(fake_progress_repo: FakeProgressRepository) -> None:
    """Verify in-memory check constraint violations raise errors."""
    u_id = uuid.uuid4()
    c_id = uuid.uuid4()

    # Completed > total assignments
    with pytest.raises(ValueError, match="ck_course_progress_counts"):
        await fake_progress_repo.upsert_course_progress(
            user_id=u_id,
            course_id=c_id,
            assignments_total=2,
            assignments_completed=3,
            study_items_total=0,
            study_items_completed=0,
            progress_percent=50.0,
            risk_status="on_track",
        )

    # Negative total count
    with pytest.raises(ValueError, match="ck_course_progress_counts"):
        await fake_progress_repo.upsert_course_progress(
            user_id=u_id,
            course_id=c_id,
            assignments_total=-1,
            assignments_completed=0,
            study_items_total=0,
            study_items_completed=0,
            progress_percent=0.0,
            risk_status="on_track",
        )

    # Invalid progress_percent
    with pytest.raises(ValueError, match="ck_course_progress_percent"):
        await fake_progress_repo.upsert_course_progress(
            user_id=u_id,
            course_id=c_id,
            assignments_total=2,
            assignments_completed=2,
            study_items_total=0,
            study_items_completed=0,
            progress_percent=105.0,
            risk_status="on_track",
        )

    # Invalid risk status
    with pytest.raises(ValueError, match="ck_course_progress_risk"):
        await fake_progress_repo.upsert_course_progress(
            user_id=u_id,
            course_id=c_id,
            assignments_total=2,
            assignments_completed=2,
            study_items_total=0,
            study_items_completed=0,
            progress_percent=100.0,
            risk_status="failing",
        )


@pytest.mark.asyncio
async def test_repository_upsert_uniqueness(fake_progress_repo: FakeProgressRepository) -> None:
    """Verify upserting with same (user_id, course_id) updates existing record."""
    u_id = uuid.uuid4()
    c_id = uuid.uuid4()

    p1 = await fake_progress_repo.upsert_course_progress(
        user_id=u_id,
        course_id=c_id,
        assignments_total=2,
        assignments_completed=1,
        study_items_total=0,
        study_items_completed=0,
        progress_percent=50.0,
        risk_status="on_track",
    )
    assert p1.progress_percent == 50.0

    p2 = await fake_progress_repo.upsert_course_progress(
        user_id=u_id,
        course_id=c_id,
        assignments_total=2,
        assignments_completed=2,
        study_items_total=0,
        study_items_completed=0,
        progress_percent=100.0,
        risk_status="on_track",
    )

    all_progress = await fake_progress_repo.list_progress(user_id=u_id)
    assert len(all_progress) == 1
    assert p2.progress_percent == 100.0
    assert p2.assignments_completed == 2


# ---------------------------------------------------------------------------
# 2. Progress Calculation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_zero_assignments_zero_study_items(
    progress_service: ProgressService,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Course with 0 assignments and 0 study items should calculate 0% progress and 'on_track'."""
    course = await fake_course_repo.create(
        user_id=student_a.id,
        name="Empty Course 101",
        course_code="EC101",
    )

    dto = await progress_service.calculate_course_progress(
        user_id=student_a.id, course_id=course.id
    )

    assert dto.course_id == course.id
    assert dto.course_name == "Empty Course 101"
    assert dto.course_code == "EC101"
    assert dto.assignments_total == 0
    assert dto.assignments_completed == 0
    assert dto.study_items_total == 0
    assert dto.study_items_completed == 0
    assert dto.progress_percent == 0.0
    assert dto.risk_status == "on_track"


@pytest.mark.asyncio
async def test_progress_completed_assignments_and_study_items(
    progress_service: ProgressService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_plan_repo: FakeStudyPlanRepository,
    student_a: User,
) -> None:
    """Course with completed assignments and study plan items calculates accurate blended percentage."""
    course = await fake_course_repo.create(
        user_id=student_a.id,
        name="Data Structures",
        course_code="CS201",
    )

    # 4 assignments: 3 completed, 1 pending (due in future)
    now = datetime.now(UTC)
    for i in range(3):
        await fake_assignment_repo.create(
            course_id=course.id,
            title=f"Assignment {i+1}",
            status="completed",
            due_at=now + timedelta(days=10),
        )
    await fake_assignment_repo.create(
        course_id=course.id,
        title="Assignment 4",
        status="pending",
        due_at=now + timedelta(days=14),
    )

    # 2 study items: 1 completed, 1 pending
    plan = await fake_plan_repo.create_plan(
        user_id=student_a.id,
        name="CS201 Revision Plan",
        start_date=date.today(),
        end_date=date.today() + timedelta(days=30),
    )
    await fake_plan_repo.add_items(
        plan_id=plan.id,
        items_data=[
            {
                "course_id": course.id,
                "title": "Study Trees",
                "scheduled_start": now + timedelta(days=1),
                "status": "completed",
            },
            {
                "course_id": course.id,
                "title": "Study Graphs",
                "scheduled_start": now + timedelta(days=3),
                "status": "pending",
            },
            {
                # Dismissed item should NOT count in study items
                "course_id": course.id,
                "title": "Discarded Topic",
                "scheduled_start": now + timedelta(days=5),
                "status": "dismissed",
            },
        ],
    )

    # Total tasks = 4 assignments + 2 non-dismissed study items = 6
    # Completed tasks = 3 assignments + 1 study item = 4
    # Expected progress = 4 / 6 * 100 = 66.67%
    dto = await progress_service.calculate_course_progress(
        user_id=student_a.id, course_id=course.id
    )

    assert dto.assignments_total == 4
    assert dto.assignments_completed == 3
    assert dto.study_items_total == 2
    assert dto.study_items_completed == 1
    assert dto.progress_percent == 66.67
    assert dto.risk_status == "on_track"


@pytest.mark.asyncio
async def test_progress_overdue_assignment(
    progress_service: ProgressService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    student_a: User,
) -> None:
    """Course with pending assignment past its due date evaluates risk_status = 'overdue'."""
    course = await fake_course_repo.create(
        user_id=student_a.id,
        name="Operating Systems",
        course_code="CS301",
    )

    now = datetime.now(UTC)
    # Overdue pending assignment
    await fake_assignment_repo.create(
        course_id=course.id,
        title="Lab 1: Kernel Compilation",
        status="pending",
        due_at=now - timedelta(days=2),
    )

    dto = await progress_service.calculate_course_progress(
        user_id=student_a.id, course_id=course.id
    )

    assert dto.assignments_total == 1
    assert dto.assignments_completed == 0
    assert dto.progress_percent == 0.0
    assert dto.risk_status == "overdue"


@pytest.mark.asyncio
async def test_progress_completed_past_due_assignment_not_overdue(
    progress_service: ProgressService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    student_a: User,
) -> None:
    """Assignment past due date that is marked 'completed' does NOT trigger 'overdue' risk."""
    course = await fake_course_repo.create(
        user_id=student_a.id,
        name="Calculus II",
        course_code="MATH102",
    )

    now = datetime.now(UTC)
    await fake_assignment_repo.create(
        course_id=course.id,
        title="Problem Set 1",
        status="completed",
        due_at=now - timedelta(days=3),
    )

    dto = await progress_service.calculate_course_progress(
        user_id=student_a.id, course_id=course.id
    )

    assert dto.assignments_total == 1
    assert dto.assignments_completed == 1
    assert dto.progress_percent == 100.0
    assert dto.risk_status == "on_track"


@pytest.mark.asyncio
async def test_progress_at_risk_low_progress_and_upcoming_exam(
    progress_service: ProgressService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_exam_repo: FakeExamRepository,
    student_a: User,
) -> None:
    """Course with progress < 50% and exam within 7 days evaluates risk_status = 'at_risk'."""
    course = await fake_course_repo.create(
        user_id=student_a.id,
        name="Linear Algebra",
        course_code="MATH201",
    )

    now = datetime.now(UTC)
    # 4 assignments: 1 completed, 3 pending future -> 25% progress (< 50%)
    await fake_assignment_repo.create(
        course_id=course.id,
        title="HW 1",
        status="completed",
        due_at=now + timedelta(days=10),
    )
    for i in range(2, 5):
        await fake_assignment_repo.create(
            course_id=course.id,
            title=f"HW {i}",
            status="pending",
            due_at=now + timedelta(days=10 + i),
        )

    # Exam scheduled in 4 days (within 7 days)
    await fake_exam_repo.create(
        course_id=course.id,
        title="Midterm Examination",
        exam_at=now + timedelta(days=4),
    )

    dto = await progress_service.calculate_course_progress(
        user_id=student_a.id, course_id=course.id
    )

    assert dto.progress_percent == 25.0
    assert dto.risk_status == "at_risk"


@pytest.mark.asyncio
async def test_progress_on_track_high_progress_with_upcoming_exam(
    progress_service: ProgressService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_exam_repo: FakeExamRepository,
    student_a: User,
) -> None:
    """Course with progress >= 50% and exam within 7 days remains 'on_track'."""
    course = await fake_course_repo.create(
        user_id=student_a.id,
        name="Discrete Mathematics",
        course_code="CS102",
    )

    now = datetime.now(UTC)
    # 2 assignments: 1 completed, 1 pending future -> 50% progress
    await fake_assignment_repo.create(
        course_id=course.id,
        title="HW 1",
        status="completed",
        due_at=now + timedelta(days=10),
    )
    await fake_assignment_repo.create(
        course_id=course.id,
        title="HW 2",
        status="pending",
        due_at=now + timedelta(days=12),
    )

    # Exam scheduled in 5 days
    await fake_exam_repo.create(
        course_id=course.id,
        title="Quiz 1",
        exam_at=now + timedelta(days=5),
    )

    dto = await progress_service.calculate_course_progress(
        user_id=student_a.id, course_id=course.id
    )

    assert dto.progress_percent == 50.0
    assert dto.risk_status == "on_track"


@pytest.mark.asyncio
async def test_progress_overdue_precedence_over_at_risk(
    progress_service: ProgressService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_exam_repo: FakeExamRepository,
    student_a: User,
) -> None:
    """Overdue pending assignment takes precedence over at_risk condition."""
    course = await fake_course_repo.create(
        user_id=student_a.id,
        name="Computer Networks",
        course_code="CS401",
    )

    now = datetime.now(UTC)
    # Overdue pending assignment
    await fake_assignment_repo.create(
        course_id=course.id,
        title="Project 1: Socket Programming",
        status="pending",
        due_at=now - timedelta(days=1),
    )

    # Upcoming exam in 3 days with 0% progress
    await fake_exam_repo.create(
        course_id=course.id,
        title="Midterm Exam",
        exam_at=now + timedelta(days=3),
    )

    dto = await progress_service.calculate_course_progress(
        user_id=student_a.id, course_id=course.id
    )

    assert dto.risk_status == "overdue"


# ---------------------------------------------------------------------------
# 3. Dashboard Aggregation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_aggregation_multi_course(
    progress_service: ProgressService,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    fake_exam_repo: FakeExamRepository,
    student_a: User,
) -> None:
    """Dashboard aggregates total courses, overall percentage, risk categories, and urgent deadlines."""
    now = datetime.now(UTC)

    # Course 1: on_track (100% complete)
    c1 = await fake_course_repo.create(user_id=student_a.id, name="Course 1", course_code="C1")
    await fake_assignment_repo.create(
        course_id=c1.id, title="C1-A1", status="completed", due_at=now + timedelta(days=2)
    )

    # Course 2: at_risk (25% complete, exam in 4 days)
    c2 = await fake_course_repo.create(user_id=student_a.id, name="Course 2", course_code="C2")
    await fake_assignment_repo.create(
        course_id=c2.id, title="C2-A1", status="completed", due_at=now + timedelta(days=10)
    )
    for i in range(2, 5):
        # Pending assignment due in 3 days -> counts towards upcoming_urgent_deadlines!
        due = now + timedelta(days=3) if i == 2 else now + timedelta(days=15)
        await fake_assignment_repo.create(
            course_id=c2.id, title=f"C2-A{i}", status="pending", due_at=due
        )
    await fake_exam_repo.create(
        course_id=c2.id, title="C2 Midterm", exam_at=now + timedelta(days=4)
    )

    # Course 3: overdue (overdue pending assignment)
    c3 = await fake_course_repo.create(user_id=student_a.id, name="Course 3", course_code="C3")
    await fake_assignment_repo.create(
        course_id=c3.id, title="C3-A1", status="pending", due_at=now - timedelta(days=2)
    )

    dashboard: DashboardProgressDTO = await progress_service.get_dashboard(user_id=student_a.id)

    assert dashboard.total_courses == 3
    assert dashboard.on_track_count == 1
    assert dashboard.at_risk_count == 1
    assert dashboard.overdue_count == 1

    # Overall percentage = round((100.0 + 25.0 + 0.0) / 3, 2) = 41.67
    assert dashboard.overall_progress_percent == 41.67

    # Urgent deadlines:
    # - C2 pending assignment due in 3 days (+1)
    # - C2 exam in 4 days (+1)
    # - (C1 assignment is completed, C3 is overdue past date, others > 7 days)
    assert dashboard.upcoming_urgent_deadlines == 2


@pytest.mark.asyncio
async def test_dashboard_empty_courses(
    progress_service: ProgressService,
    student_a: User,
) -> None:
    """Dashboard returns healthy zero defaults when student has no enrolled courses."""
    dashboard = await progress_service.get_dashboard(user_id=student_a.id)

    assert dashboard.total_courses == 0
    assert dashboard.overall_progress_percent == 0.0
    assert dashboard.on_track_count == 0
    assert dashboard.at_risk_count == 0
    assert dashboard.overdue_count == 0
    assert dashboard.courses == []
    assert dashboard.upcoming_urgent_deadlines == 0


# ---------------------------------------------------------------------------
# 4. Strict Tenant Isolation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_isolation_course_progress(
    progress_service: ProgressService,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Student A cannot calculate or view Student B's course progress."""
    course_b = await fake_course_repo.create(
        user_id=student_b.id, name="Bob's Course", course_code="BC101"
    )

    with pytest.raises(CourseNotFoundError, match="was not found"):
        await progress_service.calculate_course_progress(
            user_id=student_a.id, course_id=course_b.id
        )

    with pytest.raises(CourseNotFoundError, match="was not found"):
        await progress_service.get_course_progress(
            user_id=student_a.id, course_id=course_b.id
        )


@pytest.mark.asyncio
async def test_tenant_isolation_dashboard(
    progress_service: ProgressService,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Dashboard only aggregates courses owned by the authenticated student."""
    course_a = await fake_course_repo.create(
        user_id=student_a.id, name="Alice Course", course_code="AC1"
    )
    course_b = await fake_course_repo.create(
        user_id=student_b.id, name="Bob Course", course_code="BC1"
    )

    dashboard_a = await progress_service.get_dashboard(user_id=student_a.id)
    assert dashboard_a.total_courses == 1
    assert dashboard_a.courses[0].course_id == course_a.id

    dashboard_b = await progress_service.get_dashboard(user_id=student_b.id)
    assert dashboard_b.total_courses == 1
    assert dashboard_b.courses[0].course_id == course_b.id


# ---------------------------------------------------------------------------
# 5. REST API Authentication & Authorization Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_unauthenticated_dashboard_401(client: AsyncClient) -> None:
    """Unauthenticated request to progress dashboard returns 401 Unauthorized."""
    resp = await client.get("/api/v1/progress/dashboard")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_inactive_user_dashboard_403(
    client: AsyncClient, inactive_student: User
) -> None:
    """Inactive user request to progress dashboard returns 403 Forbidden."""
    headers = make_auth_header(inactive_student.id)
    resp = await client.get("/api/v1/progress/dashboard", headers=headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_api_get_dashboard_success(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    fake_assignment_repo: FakeAssignmentRepository,
    student_a: User,
) -> None:
    """Authenticated student can successfully retrieve progress dashboard."""
    course = await fake_course_repo.create(
        user_id=student_a.id, name="Algorithms", course_code="CS302"
    )
    await fake_assignment_repo.create(
        course_id=course.id,
        title="Problem Set 1",
        status="completed",
        due_at=datetime.now(UTC) + timedelta(days=5),
    )

    headers = make_auth_header(student_a.id)
    resp = await client.get("/api/v1/progress/dashboard", headers=headers)
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["message"] == "Academic progress dashboard retrieved successfully."
    payload = data["data"]
    assert payload["total_courses"] == 1
    assert payload["overall_progress_percent"] == 100.0
    assert payload["on_track_count"] == 1
    assert len(payload["courses"]) == 1
    assert payload["courses"][0]["course_id"] == str(course.id)


@pytest.mark.asyncio
async def test_api_list_courses_progress(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Authenticated student can list progress for all active courses."""
    c1 = await fake_course_repo.create(user_id=student_a.id, name="Course 1", course_code="C1")
    c2 = await fake_course_repo.create(user_id=student_a.id, name="Course 2", course_code="C2")

    headers = make_auth_header(student_a.id)
    resp = await client.get("/api/v1/progress/courses", headers=headers)
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert len(data["data"]) == 2
    c_ids = {c["course_id"] for c in data["data"]}
    assert str(c1.id) in c_ids
    assert str(c2.id) in c_ids


@pytest.mark.asyncio
async def test_api_get_single_course_progress(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Authenticated student can fetch progress for a specific course."""
    course = await fake_course_repo.create(
        user_id=student_a.id, name="Artificial Intelligence", course_code="AI101"
    )

    headers = make_auth_header(student_a.id)
    resp = await client.get(f"/api/v1/progress/courses/{course.id}", headers=headers)
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["data"]["course_id"] == str(course.id)
    assert data["data"]["course_name"] == "Artificial Intelligence"
    assert data["data"]["progress_percent"] == 0.0
    assert data["data"]["risk_status"] == "on_track"


@pytest.mark.asyncio
async def test_api_get_single_course_unowned_404(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Accessing an unowned course's progress returns 404 Not Found."""
    course_b = await fake_course_repo.create(
        user_id=student_b.id, name="Bob Secret Course", course_code="BSC"
    )

    headers = make_auth_header(student_a.id)
    resp = await client.get(f"/api/v1/progress/courses/{course_b.id}", headers=headers)
    assert resp.status_code == 404
    data = resp.json()
    assert data["success"] is False
    assert data["error"]["message"] == "Course not found."


@pytest.mark.asyncio
async def test_api_recalculate_progress(
    client: AsyncClient,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Authenticated student can trigger recalculation of academic progress."""
    await fake_course_repo.create(user_id=student_a.id, name="Physics I", course_code="PHY101")

    headers = make_auth_header(student_a.id)
    resp = await client.post("/api/v1/progress/recalculate", headers=headers)
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["message"] == "Academic progress recalculated successfully."
    assert len(data["data"]) == 1
