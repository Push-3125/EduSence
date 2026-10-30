"""Comprehensive tests for Study Plan Item Acceptance, Dismissal & Calendar Sync (TASK-M5-06 / US 2.16 / US 2.17).

Validates:
- Invariant 2 (Layering): Routers -> Services -> Repositories.
- Invariant 6 (Strict Tenant Isolation): User A cannot accept, dismiss, complete, or bulk-update User B's plan items.
- Database Constraints:
  * ck_study_plan_items_status: status IN ('pending', 'accepted', 'in_progress', 'completed', 'dismissed')
  * ck_study_plan_items_accepted_at: status NOT IN ('accepted', 'in_progress', 'completed') OR accepted_at IS NOT NULL
  * ck_study_plan_items_completed_at: status <> 'completed' OR completed_at IS NOT NULL
  * ck_study_plan_items_time: scheduled_end IS NULL OR scheduled_end > scheduled_start
- Item Lifecycle:
  * Accepting item sets status='accepted' and accepted_at=now().
  * Dismissing item sets status='dismissed'.
  * Completing item sets status='completed', completed_at=now(), and populates accepted_at if missing.
  * Bulk accept and bulk dismiss update only pending items.
- Calendar Sync:
  * Accepted, in-progress, and completed study plan items appear in get_aggregated_calendar with color="#10B981".
  * Dismissed and pending items do NOT appear in the calendar.
  * Scoped strictly by student user_id and query date range.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
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
from sqlalchemy import CheckConstraint

from app.api.v1.calendar import router as calendar_router
from app.api.v1.study_plans import router as study_plans_router
from app.core.security import create_access_token, hash_password
from app.main import app
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.usage_logger import AIUsageLogger
from app.repositories.assignment_repo import AssignmentRepository
from app.repositories.course_repo import Course, CourseRepository, get_course_repo
from app.repositories.exam_repo import ExamRepository
from app.repositories.schedule_repo import ScheduleRepository
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
from app.services.calendar_service import (
    CalendarService,
    get_calendar_service,
)
from app.services.quota_service import AIQuotaService
from app.services.sanitizer import PromptSanitizer
from app.services.study_plan_service import (
    StudyPlanService,
    get_study_plan_service,
)

# Ensure routers are mounted
if not any(getattr(r, "path", "").startswith("/api/v1/study-plans") for r in app.routes):
    app.include_router(study_plans_router)
if not any(getattr(r, "path", "").startswith("/api/v1/calendar") for r in app.routes):
    app.include_router(calendar_router)


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
        all_c = await self.list_all_by_user(user_id)
        if status:
            all_c = [c for c in all_c if c.status == status]
        return all_c[skip : skip + limit], len(all_c)


class FakeStudyPlanRepository(StudyPlanRepository):
    """In-memory StudyPlanRepository double supporting all TASK-M5-06 operations."""

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
        now = datetime.now(UTC)
        plan_id = uuid.uuid4()
        plan = StudyPlan(
            id=plan_id,
            user_id=user_id,
            name=name.strip(),
            start_date=start_date,
            end_date=end_date,
            description=description.strip() if description else None,
            generated_by_ai=generated_by_ai,
            status=status,
            created_at=now,
            updated_at=now,
        )
        plan.items = []
        self.plans[plan_id] = plan
        return plan

    async def get_plan(
        self, user_id: uuid.UUID, plan_id: uuid.UUID
    ) -> StudyPlan | None:
        plan = self.plans.get(plan_id)
        if plan is None or plan.user_id != user_id:
            return None
        plan_items = [it for it in self.items.values() if it.study_plan_id == plan_id]
        plan_items.sort(key=lambda x: (x.scheduled_start, x.display_order))
        plan.items = plan_items
        return plan

    async def add_items(
        self, plan_id: uuid.UUID, items_data: list[dict[str, Any]]
    ) -> list[StudyPlanItem]:
        created: list[StudyPlanItem] = []
        now = datetime.now(UTC)
        for d in items_data:
            item_id = uuid.uuid4()
            item = StudyPlanItem(
                id=item_id,
                study_plan_id=plan_id,
                course_id=d.get("course_id"),
                title=d["title"],
                description=d.get("description"),
                scheduled_start=d["scheduled_start"],
                scheduled_end=d.get("scheduled_end"),
                status=d.get("status", "pending"),
                completed_at=d.get("completed_at"),
                accepted_at=d.get("accepted_at"),
                display_order=d.get("display_order", 0),
                created_at=now,
                updated_at=now,
            )
            self.items[item_id] = item
            created.append(item)
        return created

    async def get_item(
        self, user_id: uuid.UUID, plan_id: uuid.UUID, item_id: uuid.UUID
    ) -> StudyPlanItem | None:
        plan = self.plans.get(plan_id)
        if plan is None or plan.user_id != user_id:
            return None
        item = self.items.get(item_id)
        if item is None or item.study_plan_id != plan_id:
            return None
        return item

    async def update_item_status(
        self,
        user_id: uuid.UUID,
        plan_id: uuid.UUID,
        item_id: uuid.UUID,
        status: str,
        accepted_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> StudyPlanItem | None:
        item = await self.get_item(user_id=user_id, plan_id=plan_id, item_id=item_id)
        if item is None:
            return None

        now = datetime.now(UTC)
        item.status = status

        if accepted_at is not None:
            item.accepted_at = accepted_at
        elif status in ("accepted", "in_progress", "completed") and item.accepted_at is None:
            item.accepted_at = now

        if completed_at is not None:
            item.completed_at = completed_at
        elif status == "completed" and item.completed_at is None:
            item.completed_at = now

        item.updated_at = now
        return item

    async def bulk_update_status(
        self,
        user_id: uuid.UUID,
        plan_id: uuid.UUID,
        target_status: str,
        item_ids: list[uuid.UUID] | None = None,
    ) -> int:
        plan = await self.get_plan(user_id=user_id, plan_id=plan_id)
        if plan is None:
            return 0

        if item_ids is not None and not item_ids:
            return 0

        now = datetime.now(UTC)
        count = 0
        for item in self.items.values():
            if item.study_plan_id != plan_id:
                continue
            if item.status != "pending":
                continue
            if item_ids is not None and item.id not in item_ids:
                continue

            item.status = target_status
            if target_status == "accepted" and item.accepted_at is None:
                item.accepted_at = now
            item.updated_at = now
            count += 1

        return count

    async def list_accepted_items_in_range(
        self,
        user_id: uuid.UUID,
        range_start: datetime,
        range_end: datetime,
    ) -> list[StudyPlanItem]:
        start_dt = range_start if range_start.tzinfo else range_start.replace(tzinfo=UTC)
        end_dt = range_end if range_end.tzinfo else range_end.replace(tzinfo=UTC)

        user_plan_ids = {p.id for p in self.plans.values() if p.user_id == user_id}
        matched: list[StudyPlanItem] = []

        for item in self.items.values():
            if item.study_plan_id not in user_plan_ids:
                continue
            if item.status not in ("accepted", "in_progress", "completed"):
                continue

            i_start = (
                item.scheduled_start
                if item.scheduled_start.tzinfo
                else item.scheduled_start.replace(tzinfo=UTC)
            )
            i_end = (
                item.scheduled_end
                if item.scheduled_end and item.scheduled_end.tzinfo
                else (item.scheduled_end.replace(tzinfo=UTC) if item.scheduled_end else None)
            )

            # Condition: scheduled_start <= range_end AND (scheduled_end >= range_start OR scheduled_start >= range_start)
            if i_start <= end_dt and (
                (i_end is not None and i_end >= start_dt) or i_start >= start_dt
            ):
                matched.append(item)

        matched.sort(key=lambda x: (x.scheduled_start, x.display_order))
        return matched


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
def fake_plan_repo() -> FakeStudyPlanRepository:
    return FakeStudyPlanRepository()


@pytest.fixture
def mock_assignment_repo() -> AsyncMock:
    repo = AsyncMock(spec=AssignmentRepository)
    repo.list_by_user = AsyncMock(return_value=([], 0))
    repo.list_by_date_range = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_exam_repo() -> AsyncMock:
    repo = AsyncMock(spec=ExamRepository)
    repo.list_by_user = AsyncMock(return_value=([], 0))
    repo.list_by_date_range = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_schedule_repo() -> AsyncMock:
    repo = AsyncMock(spec=ScheduleRepository)
    repo.list_sessions_by_user = AsyncMock(return_value=[])
    repo.list_sessions_by_date_range = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def calendar_service(
    fake_course_repo: FakeCourseRepository,
    mock_assignment_repo: AsyncMock,
    mock_exam_repo: AsyncMock,
    mock_schedule_repo: AsyncMock,
    fake_plan_repo: FakeStudyPlanRepository,
) -> CalendarService:
    return CalendarService(
        course_repo=fake_course_repo,
        assignment_repo=mock_assignment_repo,
        exam_repo=mock_exam_repo,
        schedule_repo=mock_schedule_repo,
        study_plan_repo=fake_plan_repo,
    )


@pytest.fixture
def study_plan_service(
    fake_plan_repo: FakeStudyPlanRepository,
    calendar_service: CalendarService,
) -> StudyPlanService:
    mock_gemini = AsyncMock(spec=GeminiAdapter)
    mock_sanitizer = PromptSanitizer()
    mock_quota = AsyncMock(spec=AIQuotaService)
    mock_logger = AsyncMock(spec=AIUsageLogger)
    return StudyPlanService(
        study_plan_repo=fake_plan_repo,
        calendar_service=calendar_service,
        gemini_adapter=mock_gemini,
        sanitizer=mock_sanitizer,
        quota_service=mock_quota,
        usage_logger=mock_logger,
    )


@pytest_asyncio.fixture
async def student_a(fake_user_repo: FakeUserRepository) -> User:
    return await fake_user_repo.create_user(
        email="student.a@university.edu",
        password_hash=hash_password("Pass12345!"),
        full_name="Student Alice",
    )


@pytest_asyncio.fixture
async def student_b(fake_user_repo: FakeUserRepository) -> User:
    return await fake_user_repo.create_user(
        email="student.b@university.edu",
        password_hash=hash_password("Pass12345!"),
        full_name="Student Bob",
    )


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_plan_repo: FakeStudyPlanRepository,
    calendar_service: CalendarService,
    study_plan_service: StudyPlanService,
    fake_course_repo: FakeCourseRepository,
) -> AsyncGenerator[AsyncClient, None]:
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_study_plan_repo] = lambda: fake_plan_repo
    app.dependency_overrides[get_study_plan_service] = lambda: study_plan_service
    app.dependency_overrides[get_calendar_service] = lambda: calendar_service
    app.dependency_overrides[get_course_repo] = lambda: fake_course_repo

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_header(user_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(subject=str(user_id), expires_delta=timedelta(minutes=60))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Database Check Constraints Verification
# ---------------------------------------------------------------------------


def test_database_check_constraints_verification() -> None:
    """Verify that StudyPlanItem model declares exact check constraints required by PostgreSQL schema."""
    item_table: Any = StudyPlanItem.__table__
    constraint_names = {c.name for c in item_table.constraints}

    assert "ck_study_plan_items_status" in constraint_names
    assert "ck_study_plan_items_accepted_at" in constraint_names
    assert "ck_study_plan_items_completed_at" in constraint_names
    assert "ck_study_plan_items_time" in constraint_names

    status_check = next(c for c in item_table.constraints if c.name == "ck_study_plan_items_status")
    assert isinstance(status_check, CheckConstraint)
    sql_text = str(status_check.sqltext)
    for expected_status in ("pending", "accepted", "in_progress", "completed", "dismissed"):
        assert expected_status in sql_text

    accepted_check = next(c for c in item_table.constraints if c.name == "ck_study_plan_items_accepted_at")
    assert isinstance(accepted_check, CheckConstraint)
    assert "accepted_at IS NOT NULL" in str(accepted_check.sqltext)

    completed_check = next(c for c in item_table.constraints if c.name == "ck_study_plan_items_completed_at")
    assert isinstance(completed_check, CheckConstraint)
    assert "completed_at IS NOT NULL" in str(completed_check.sqltext)

    time_check = next(c for c in item_table.constraints if c.name == "ck_study_plan_items_time")
    assert isinstance(time_check, CheckConstraint)
    assert "scheduled_end > scheduled_start" in str(time_check.sqltext)


# ---------------------------------------------------------------------------
# 2. Repository Layer Tests (Real StudyPlanRepository with Mock Session)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_update_item_status_and_bulk() -> None:
    """Test StudyPlanRepository methods interacting with SQLAlchemy AsyncSession."""
    user_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    item_id = uuid.uuid4()

    mock_plan = StudyPlan(
        id=plan_id,
        user_id=user_id,
        name="Exam Revision",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 10),
    )
    assert mock_plan.id == plan_id
    mock_item = StudyPlanItem(
        id=item_id,
        study_plan_id=plan_id,
        title="Chapter 1 Review",
        scheduled_start=datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
        status="pending",
        accepted_at=None,
        completed_at=None,
    )

    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()

    # Mock execute for get_item (scalar_one_or_none)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_item
    mock_session.execute = AsyncMock(return_value=mock_result)

    repo = StudyPlanRepository(session=mock_session)

    # 1. Accept item
    updated = await repo.update_item_status(
        user_id=user_id, plan_id=plan_id, item_id=item_id, status="accepted"
    )
    assert updated is not None
    assert updated.status == "accepted"
    assert updated.accepted_at is not None
    mock_session.commit.assert_called()

    # 2. Complete item
    updated_complete = await repo.update_item_status(
        user_id=user_id, plan_id=plan_id, item_id=item_id, status="completed"
    )
    assert updated_complete is not None
    assert updated_complete.status == "completed"
    assert updated_complete.completed_at is not None

    # 3. Dismiss item
    updated_dismiss = await repo.update_item_status(
        user_id=user_id, plan_id=plan_id, item_id=item_id, status="dismissed"
    )
    assert updated_dismiss is not None
    assert updated_dismiss.status == "dismissed"


# ---------------------------------------------------------------------------
# 3. Service Layer Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_accept_dismiss_complete_item(
    study_plan_service: StudyPlanService,
    fake_plan_repo: FakeStudyPlanRepository,
    student_a: User,
) -> None:
    """Verify accept_item, dismiss_item, and complete_item update states accurately."""
    plan = await fake_plan_repo.create_plan(
        user_id=student_a.id,
        name="Midterm Prep",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 7),
    )
    items = await fake_plan_repo.add_items(
        plan_id=plan.id,
        items_data=[
            {
                "title": "Review Trees",
                "scheduled_start": datetime(2026, 11, 2, 10, 0, tzinfo=UTC),
                "scheduled_end": datetime(2026, 11, 2, 11, 30, tzinfo=UTC),
                "status": "pending",
            },
            {
                "title": "Review Graphs",
                "scheduled_start": datetime(2026, 11, 3, 10, 0, tzinfo=UTC),
                "scheduled_end": datetime(2026, 11, 3, 11, 30, tzinfo=UTC),
                "status": "pending",
            },
            {
                "title": "Review Sorting",
                "scheduled_start": datetime(2026, 11, 4, 10, 0, tzinfo=UTC),
                "scheduled_end": datetime(2026, 11, 4, 11, 30, tzinfo=UTC),
                "status": "pending",
            },
        ],
    )

    # 1. Accept item 1
    accepted_dto = await study_plan_service.accept_item(
        user_id=student_a.id, plan_id=plan.id, item_id=items[0].id
    )
    assert accepted_dto.status == "accepted"
    assert accepted_dto.accepted_at is not None

    # 2. Dismiss item 2
    dismissed_dto = await study_plan_service.dismiss_item(
        user_id=student_a.id, plan_id=plan.id, item_id=items[1].id
    )
    assert dismissed_dto.status == "dismissed"

    # 3. Complete item 3 (directly from pending)
    completed_dto = await study_plan_service.complete_item(
        user_id=student_a.id, plan_id=plan.id, item_id=items[2].id
    )
    assert completed_dto.status == "completed"
    assert completed_dto.completed_at is not None
    assert completed_dto.accepted_at is not None  # Automatically populated to satisfy check constraint


@pytest.mark.asyncio
async def test_service_bulk_action_accept_and_dismiss(
    study_plan_service: StudyPlanService,
    fake_plan_repo: FakeStudyPlanRepository,
    student_a: User,
) -> None:
    """Verify bulk_action accepts and dismisses only pending items and respects item_ids filter."""
    plan = await fake_plan_repo.create_plan(
        user_id=student_a.id,
        name="Finals Sprint",
        start_date=date(2026, 12, 1),
        end_date=date(2026, 12, 10),
    )
    items = await fake_plan_repo.add_items(
        plan_id=plan.id,
        items_data=[
            {
                "title": f"Session {i}",
                "scheduled_start": datetime(2026, 12, 2, 9 + i, 0, tzinfo=UTC),
                "status": "pending",
            }
            for i in range(4)
        ],
    )

    # Bulk dismiss first 2 items with item_ids filter
    dismissed_count = await study_plan_service.bulk_action(
        user_id=student_a.id,
        plan_id=plan.id,
        action="dismiss",
        item_ids=[items[0].id, items[1].id],
    )
    assert dismissed_count == 2
    assert fake_plan_repo.items[items[0].id].status == "dismissed"
    assert fake_plan_repo.items[items[1].id].status == "dismissed"
    assert fake_plan_repo.items[items[2].id].status == "pending"

    # Bulk accept remaining items (item_ids=None -> all pending items)
    accepted_count = await study_plan_service.bulk_action(
        user_id=student_a.id,
        plan_id=plan.id,
        action="accept",
    )
    assert accepted_count == 2
    assert fake_plan_repo.items[items[2].id].status == "accepted"
    assert fake_plan_repo.items[items[2].id].accepted_at is not None
    assert fake_plan_repo.items[items[3].id].status == "accepted"
    assert fake_plan_repo.items[items[3].id].accepted_at is not None

    # Invalid action raises ValueError
    with pytest.raises(ValueError, match="Invalid bulk action"):
        await study_plan_service.bulk_action(
            user_id=student_a.id, plan_id=plan.id, action="invalid_action"
        )


# ---------------------------------------------------------------------------
# 4. API Endpoint Tests (Accept, Dismiss, Complete, Bulk)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_accept_item_endpoint(
    client: AsyncClient,
    fake_plan_repo: FakeStudyPlanRepository,
    student_a: User,
) -> None:
    """Test POST /api/v1/study-plans/{plan_id}/items/{item_id}/accept returns 200 with accepted_at."""
    plan = await fake_plan_repo.create_plan(
        user_id=student_a.id,
        name="AI Study",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 5),
    )
    items = await fake_plan_repo.add_items(
        plan_id=plan.id,
        items_data=[
            {
                "title": "Machine Learning Lab",
                "scheduled_start": datetime(2026, 11, 2, 14, 0, tzinfo=UTC),
                "status": "pending",
            }
        ],
    )
    item_id = items[0].id
    headers = make_auth_header(student_a.id)

    res = await client.post(
        f"/api/v1/study-plans/{plan.id}/items/{item_id}/accept",
        headers=headers,
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["success"] is True
    assert payload["data"]["status"] == "accepted"
    assert payload["data"]["accepted_at"] is not None


@pytest.mark.asyncio
async def test_api_dismiss_item_endpoint(
    client: AsyncClient,
    fake_plan_repo: FakeStudyPlanRepository,
    student_a: User,
) -> None:
    """Test POST /api/v1/study-plans/{plan_id}/items/{item_id}/dismiss returns 200."""
    plan = await fake_plan_repo.create_plan(
        user_id=student_a.id,
        name="AI Study",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 5),
    )
    items = await fake_plan_repo.add_items(
        plan_id=plan.id,
        items_data=[
            {
                "title": "Optional Exercise",
                "scheduled_start": datetime(2026, 11, 2, 16, 0, tzinfo=UTC),
                "status": "pending",
            }
        ],
    )
    item_id = items[0].id
    headers = make_auth_header(student_a.id)

    res = await client.post(
        f"/api/v1/study-plans/{plan.id}/items/{item_id}/dismiss",
        headers=headers,
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["success"] is True
    assert payload["data"]["status"] == "dismissed"


@pytest.mark.asyncio
async def test_api_complete_item_endpoint(
    client: AsyncClient,
    fake_plan_repo: FakeStudyPlanRepository,
    student_a: User,
) -> None:
    """Test POST /api/v1/study-plans/{plan_id}/items/{item_id}/complete returns 200 with completed_at."""
    plan = await fake_plan_repo.create_plan(
        user_id=student_a.id,
        name="AI Study",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 5),
    )
    items = await fake_plan_repo.add_items(
        plan_id=plan.id,
        items_data=[
            {
                "title": "Practice Problems",
                "scheduled_start": datetime(2026, 11, 3, 10, 0, tzinfo=UTC),
                "status": "accepted",
                "accepted_at": datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
            }
        ],
    )
    item_id = items[0].id
    headers = make_auth_header(student_a.id)

    res = await client.post(
        f"/api/v1/study-plans/{plan.id}/items/{item_id}/complete",
        headers=headers,
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["success"] is True
    assert payload["data"]["status"] == "completed"
    assert payload["data"]["completed_at"] is not None


@pytest.mark.asyncio
async def test_api_bulk_action_accept_and_dismiss(
    client: AsyncClient,
    fake_plan_repo: FakeStudyPlanRepository,
    student_a: User,
) -> None:
    """Test POST /api/v1/study-plans/{plan_id}/items/bulk endpoint."""
    plan = await fake_plan_repo.create_plan(
        user_id=student_a.id,
        name="Sprint Plan",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 5),
    )
    await fake_plan_repo.add_items(
        plan_id=plan.id,
        items_data=[
            {
                "title": f"Session {i}",
                "scheduled_start": datetime(2026, 11, 2, 9 + i, 0, tzinfo=UTC),
                "status": "pending",
            }
            for i in range(3)
        ],
    )
    headers = make_auth_header(student_a.id)

    # 1. Bulk accept all
    res = await client.post(
        f"/api/v1/study-plans/{plan.id}/items/bulk",
        headers=headers,
        json={"action": "accept"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["data"]["updated_count"] == 3

    # 2. Add 2 more pending items
    new_items = await fake_plan_repo.add_items(
        plan_id=plan.id,
        items_data=[
            {
                "title": f"Extra {i}",
                "scheduled_start": datetime(2026, 11, 3, 9 + i, 0, tzinfo=UTC),
                "status": "pending",
            }
            for i in range(2)
        ],
    )

    # 3. Bulk dismiss with explicit item_ids
    res_dismiss = await client.post(
        f"/api/v1/study-plans/{plan.id}/items/bulk",
        headers=headers,
        json={"action": "dismiss", "item_ids": [str(new_items[0].id)]},
    )
    assert res_dismiss.status_code == 200
    assert res_dismiss.json()["data"]["updated_count"] == 1

    # 4. Invalid action returns 422
    res_invalid = await client.post(
        f"/api/v1/study-plans/{plan.id}/items/bulk",
        headers=headers,
        json={"action": "invalid_action"},
    )
    assert res_invalid.status_code == 422


# ---------------------------------------------------------------------------
# 5. Strict Tenant Isolation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_tenant_isolation_item_actions(
    client: AsyncClient,
    fake_plan_repo: FakeStudyPlanRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Verify Student B CANNOT accept, dismiss, complete, or bulk-update Student A's items."""
    # Student A owns plan
    plan_a = await fake_plan_repo.create_plan(
        user_id=student_a.id,
        name="Alice Private Plan",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 5),
    )
    items_a = await fake_plan_repo.add_items(
        plan_id=plan_a.id,
        items_data=[
            {
                "title": "Private Session",
                "scheduled_start": datetime(2026, 11, 2, 10, 0, tzinfo=UTC),
                "status": "pending",
            }
        ],
    )
    item_a_id = items_a[0].id

    # Student B headers
    headers_b = make_auth_header(student_b.id)

    # Student B tries to accept Student A's item -> 404
    res_accept = await client.post(
        f"/api/v1/study-plans/{plan_a.id}/items/{item_a_id}/accept",
        headers=headers_b,
    )
    assert res_accept.status_code == 404

    # Student B tries to dismiss Student A's item -> 404
    res_dismiss = await client.post(
        f"/api/v1/study-plans/{plan_a.id}/items/{item_a_id}/dismiss",
        headers=headers_b,
    )
    assert res_dismiss.status_code == 404

    # Student B tries to complete Student A's item -> 404
    res_complete = await client.post(
        f"/api/v1/study-plans/{plan_a.id}/items/{item_a_id}/complete",
        headers=headers_b,
    )
    assert res_complete.status_code == 404

    # Student B tries to bulk update Student A's plan -> 404
    res_bulk = await client.post(
        f"/api/v1/study-plans/{plan_a.id}/items/bulk",
        headers=headers_b,
        json={"action": "accept"},
    )
    assert res_bulk.status_code == 404


# ---------------------------------------------------------------------------
# 6. Calendar Sync & Aggregation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calendar_sync_accepted_items_appear_in_calendar(
    calendar_service: CalendarService,
    fake_plan_repo: FakeStudyPlanRepository,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Verify accepted/in_progress/completed study plan items appear in calendar with emerald color,

    while pending and dismissed items do NOT appear.
    """
    course = await fake_course_repo.create(
        user_id=student_a.id,
        name="Operating Systems",
        course_code="CS301",
    )

    plan = await fake_plan_repo.create_plan(
        user_id=student_a.id,
        name="OS Exam Plan",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 10),
    )

    # Add 4 items with different statuses
    item_pending = {
        "course_id": course.id,
        "title": "Pending Item",
        "scheduled_start": datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
        "scheduled_end": datetime(2026, 11, 2, 10, 30, tzinfo=UTC),
        "status": "pending",
    }
    item_accepted = {
        "course_id": course.id,
        "title": "Accepted Item",
        "scheduled_start": datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
        "scheduled_end": datetime(2026, 11, 3, 10, 30, tzinfo=UTC),
        "status": "accepted",
        "accepted_at": datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
    }
    item_in_progress = {
        "course_id": course.id,
        "title": "In Progress Item",
        "scheduled_start": datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
        "scheduled_end": datetime(2026, 11, 4, 10, 30, tzinfo=UTC),
        "status": "in_progress",
        "accepted_at": datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
    }
    item_dismissed = {
        "course_id": course.id,
        "title": "Dismissed Item",
        "scheduled_start": datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
        "scheduled_end": datetime(2026, 11, 5, 10, 30, tzinfo=UTC),
        "status": "dismissed",
    }
    item_completed = {
        "course_id": course.id,
        "title": "Completed Item",
        "scheduled_start": datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
        "scheduled_end": datetime(2026, 11, 6, 10, 30, tzinfo=UTC),
        "status": "completed",
        "accepted_at": datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
        "completed_at": datetime(2026, 11, 6, 11, 0, tzinfo=UTC),
    }

    await fake_plan_repo.add_items(
        plan_id=plan.id,
        items_data=[item_pending, item_accepted, item_in_progress, item_dismissed, item_completed],
    )

    calendar_data = await calendar_service.get_aggregated_calendar(
        user_id=student_a.id,
        from_date=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
        to_date=datetime(2026, 11, 10, 23, 59, tzinfo=UTC),
    )

    titles = [e.title for e in calendar_data.events]
    assert "Accepted Item" in titles
    assert "In Progress Item" in titles
    assert "Completed Item" in titles
    assert "Pending Item" not in titles
    assert "Dismissed Item" not in titles

    # Verify emerald color and DTO fields
    accepted_event = next(e for e in calendar_data.events if e.title == "Accepted Item")
    assert accepted_event.color == "#10B981"
    assert accepted_event.event_type == "study_plan_item"
    assert accepted_event.item_type == "study_plan_item"
    assert accepted_event.course_name == "Operating Systems"
    assert accepted_event.course_code == "CS301"


@pytest.mark.asyncio
async def test_calendar_sync_strict_tenant_isolation(
    calendar_service: CalendarService,
    fake_plan_repo: FakeStudyPlanRepository,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
    student_b: User,
) -> None:
    """Verify Student B's calendar does not contain Student A's accepted study plan items."""
    course_a = await fake_course_repo.create(
        user_id=student_a.id,
        name="Algorithms A",
        course_code="CS101",
    )
    plan_a = await fake_plan_repo.create_plan(
        user_id=student_a.id,
        name="Alice Plan",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 10),
    )
    await fake_plan_repo.add_items(
        plan_id=plan_a.id,
        items_data=[
            {
                "course_id": course_a.id,
                "title": "Alice Secret Study Session",
                "scheduled_start": datetime(2026, 11, 5, 10, 0, tzinfo=UTC),
                "scheduled_end": datetime(2026, 11, 5, 11, 0, tzinfo=UTC),
                "status": "accepted",
                "accepted_at": datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
            }
        ],
    )

    # Query calendar as Student B
    cal_b = await calendar_service.get_aggregated_calendar(
        user_id=student_b.id,
        from_date=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
        to_date=datetime(2026, 11, 10, 23, 59, tzinfo=UTC),
    )
    assert cal_b.total_events == 0
    assert not any(e.title == "Alice Secret Study Session" for e in cal_b.events)


@pytest.mark.asyncio
async def test_api_calendar_event_type_filter_with_study_plan_items(
    client: AsyncClient,
    fake_plan_repo: FakeStudyPlanRepository,
    fake_course_repo: FakeCourseRepository,
    student_a: User,
) -> None:
    """Test /api/v1/calendar endpoint with ?event_types=study_plan_item."""
    course = await fake_course_repo.create(
        user_id=student_a.id,
        name="Software Engineering",
        course_code="CS401",
    )
    plan = await fake_plan_repo.create_plan(
        user_id=student_a.id,
        name="Study Sprint",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 10),
    )
    await fake_plan_repo.add_items(
        plan_id=plan.id,
        items_data=[
            {
                "course_id": course.id,
                "title": "Refactoring Practice",
                "scheduled_start": datetime(2026, 11, 5, 14, 0, tzinfo=UTC),
                "scheduled_end": datetime(2026, 11, 5, 16, 0, tzinfo=UTC),
                "status": "accepted",
                "accepted_at": datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
            }
        ],
    )

    headers = make_auth_header(student_a.id)

    # 1. Query with ?event_types=study_plan_item
    res = await client.get(
        "/api/v1/calendar?from_date=2026-11-01T00:00:00Z&to_date=2026-11-10T23:59:59Z&event_types=study_plan_item",
        headers=headers,
    )
    assert res.status_code == 200
    events = res.json()["data"]["events"]
    assert len(events) == 1
    assert events[0]["title"] == "Refactoring Practice"
    assert events[0]["color"] == "#10B981"
    assert events[0]["event_type"] == "study_plan_item"

    # 2. Query with ?event_types=exam (excludes study plan items)
    res_exam = await client.get(
        "/api/v1/calendar?from_date=2026-11-01T00:00:00Z&to_date=2026-11-10T23:59:59Z&event_types=exam",
        headers=headers,
    )
    assert res_exam.status_code == 200
    assert len(res_exam.json()["data"]["events"]) == 0
