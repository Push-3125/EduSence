"""Comprehensive tests for AI Study & Revision Plan Generation (TASK-M5-05 / US 2.16).

Validates:
- Invariant 2 (Layering): Routers -> Services -> Repositories.
- Invariant 4 (Central LLM): Gemini called only through GeminiAdapter; mocks used in tests.
- Invariant 6 (Strict Tenant Isolation): User A cannot view, update, or delete User B's study plans or items.
- Invariant 7 (Quota & Telemetry): Atomic quota reservation before LLM call; telemetry logged with tokens/latency.
- Invariant 13 (Untrusted Context Fencing): Prompts fenced via PromptSanitizer.
- Database Constraints: ck_study_plans_date, ck_study_plans_status, ck_study_plan_items_time,
  ck_study_plan_items_status, ck_study_plan_items_accepted_at, ck_study_plan_items_completed_at.
- Academic Context: Calendar commitments (exams, assignments, classes) integrated into generation prompt.
- API Endpoints: Status codes (200, 201, 401, 404, 422, 429, 503, 504) and envelope structures.
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

from app.api.v1.study_plans import router as study_plans_router
from app.core.security import create_access_token
from app.main import app
from app.providers.gemini_adapter import (
    GeminiCircuitOpenError,
    GeminiRateLimitError,
    GeminiTimeoutError,
    get_gemini_adapter,
)
from app.providers.usage_logger import (
    AIUsageLogger,
    get_ai_usage_logger,
)
from app.repositories.ai_usage_repo import AIUsageLog
from app.repositories.course_repo import Course, CourseRepository
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
    CalendarAggregatedDTO,
    CalendarEventDTO,
    CalendarService,
    get_calendar_service,
)
from app.services.quota_service import (
    AIQuotaService,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    get_quota_service,
)
from app.services.sanitizer import PromptSanitizer, get_prompt_sanitizer
from app.services.study_plan_service import (
    GeneratedPlanItem,
    GeneratedPlanOutput,
    InvalidPlanDateError,
    StudyPlanService,
)

# Ensure study_plans router is mounted on app
if not any(
    getattr(route, "path", "").startswith("/api/v1/study-plans") for route in app.routes
):
    app.include_router(study_plans_router)


# ---------------------------------------------------------------------------
# Test Doubles & In-Memory Fakes
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for authentication in tests."""

    def __init__(self) -> None:
        self.users: dict[uuid.UUID, User] = {}
        self.roles: dict[str, Role] = {
            "student": Role(
                id=uuid.uuid4(),
                name="student",
                description="Default student role",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
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


class FakeStudyPlanRepository(StudyPlanRepository):
    """In-memory StudyPlanRepository double strictly enforcing tenant isolation."""

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
        # Attach matching items
        plan_items = [
            it for it in self.items.values() if it.study_plan_id == plan_id
        ]
        plan_items.sort(key=lambda x: (x.scheduled_start, x.display_order))
        plan.items = plan_items
        return plan

    async def list_plans(
        self,
        user_id: uuid.UUID,
        skip: int = 0,
        limit: int = 50,
        status: str | None = None,
    ) -> tuple[list[StudyPlan], int]:
        user_plans = [p for p in self.plans.values() if p.user_id == user_id]
        if status:
            user_plans = [p for p in user_plans if p.status == status]
        user_plans.sort(key=lambda p: (p.start_date, p.created_at), reverse=True)
        total = len(user_plans)
        paginated = user_plans[skip : skip + limit]
        for p in paginated:
            p_items = [it for it in self.items.values() if it.study_plan_id == p.id]
            p_items.sort(key=lambda x: (x.scheduled_start, x.display_order))
            p.items = p_items
        return paginated, total

    async def delete_plan(self, user_id: uuid.UUID, plan_id: uuid.UUID) -> bool:
        plan = await self.get_plan(user_id=user_id, plan_id=plan_id)
        if plan is None:
            return False
        # Remove items
        item_ids_to_remove = [
            it.id for it in self.items.values() if it.study_plan_id == plan_id
        ]
        for i_id in item_ids_to_remove:
            self.items.pop(i_id, None)
        self.plans.pop(plan_id, None)
        return True

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


class FakeCalendarService(CalendarService):
    """In-memory CalendarService double returning aggregated events and courses."""

    def __init__(self, events: list[CalendarEventDTO] | None = None) -> None:
        self.mock_events = events or []
        self.mock_courses: list[Course] = []
        # Provide a dummy course_repo attribute for course matching
        self.course_repo = MagicMock(spec=CourseRepository)
        self.course_repo.list_by_user = AsyncMock(
            side_effect=lambda user_id: (self.mock_courses, len(self.mock_courses))
        )

    async def get_aggregated_calendar(
        self,
        user_id: uuid.UUID,
        from_date: datetime,
        to_date: datetime,
        course_id: uuid.UUID | None = None,
        event_types: list[str] | None = None,
    ) -> CalendarAggregatedDTO:
        return CalendarAggregatedDTO(
            from_date=from_date,
            to_date=to_date,
            total_events=len(self.mock_events),
            events=list(self.mock_events),
        )


class FakeUsageLogger(AIUsageLogger):
    """In-memory AI telemetry logger test double."""

    def __init__(self) -> None:
        self.logged_calls: list[dict[str, Any]] = []

    async def log_ai_call(
        self,
        user_id: uuid.UUID,
        feature: str,
        model_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int | None = None,
        latency_ms: int | None = None,
        request_status: str = "success",
        error_message: str | None = None,
        conversation_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        estimated_cost: float | Any = None,
        provider: str = "google",
        repository: Any = None,
    ) -> AIUsageLog | None:
        record = {
            "user_id": user_id,
            "feature": feature,
            "model_name": model_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
            "latency_ms": latency_ms,
            "request_status": request_status,
        }
        self.logged_calls.append(record)
        return None


class FakeQuotaService(AIQuotaService):
    """In-memory quota manager supporting simulated exhaustion and fail-closed errors."""

    def __init__(self) -> None:
        self.reservations: list[dict[str, Any]] = []
        self.refunds: list[dict[str, Any]] = []
        self.is_exhausted: bool = False
        self.is_redis_down: bool = False

    async def reserve_quota(
        self,
        user_id: uuid.UUID | str,
        plan_code: str = "free",
        target_date: Any = None,
    ) -> Any:
        if self.is_redis_down:
            raise QuotaServiceUnavailableError("Simulated Redis failure (fail-closed)")
        if self.is_exhausted:
            raise QuotaExceededError("Daily AI quota limit exceeded")

        self.reservations.append({"user_id": user_id, "plan_code": plan_code})
        status = MagicMock()
        status.user_id = user_id
        status.remaining = 10
        return status

    async def refund_quota(
        self,
        user_id: uuid.UUID | str,
        target_date: Any = None,
    ) -> None:
        self.refunds.append({"user_id": user_id})


class MockGeminiAdapter:
    """Mock GeminiAdapter for structured generation testing."""

    def __init__(self, responses: list[GeneratedPlanOutput] | None = None) -> None:
        self.model = "gemini-2.5-flash"
        self.responses = responses or []
        self.call_count = 0
        self.last_prompt: str = ""
        self.last_system_instruction: str | None = None
        self.simulate_circuit_open = False
        self.simulate_rate_limit = False
        self.simulate_timeout = False

    async def agenerate_structured(
        self,
        prompt: str,
        response_schema: Any,
        system_instruction: str | None = None,
        temperature: float = 0.2,
    ) -> Any:
        self.call_count += 1
        self.last_prompt = prompt
        self.last_system_instruction = system_instruction

        if self.simulate_circuit_open:
            raise GeminiCircuitOpenError("Circuit is OPEN")
        if self.simulate_rate_limit:
            raise GeminiRateLimitError("Rate limit reached")
        if self.simulate_timeout:
            raise GeminiTimeoutError("Request timed out")

        if self.responses:
            return self.responses.pop(0)

        # Default structured study plan
        return GeneratedPlanOutput(
            plan_name="AI Exam Revision Schedule",
            rationale="Balanced preparation focusing on algorithms and calculus ahead of midterms.",
            items=[
                GeneratedPlanItem(
                    title="Review Binary Search Trees & AVL",
                    description="Study tree rotations and rebalancing conditions",
                    day_offset=1,
                    start_time="09:00",
                    duration_minutes=90,
                    course_name="Data Structures",
                ),
                GeneratedPlanItem(
                    title="Calculus Integration by Parts",
                    description="Practice trigonometric substitution and integration by parts",
                    day_offset=3,
                    start_time="14:30",
                    duration_minutes=60,
                    course_name="Calculus II",
                ),
                GeneratedPlanItem(
                    title="Mock Exam Practice Session",
                    description="Timed test covering sorting and dynamic programming",
                    day_offset=5,
                    start_time="10:00",
                    duration_minutes=120,
                    course_name="Data Structures",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_user_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def fake_plan_repo() -> FakeStudyPlanRepository:
    return FakeStudyPlanRepository()


@pytest.fixture
def fake_calendar_service() -> FakeCalendarService:
    return FakeCalendarService()


@pytest.fixture
def fake_usage_logger() -> FakeUsageLogger:
    return FakeUsageLogger()


@pytest.fixture
def fake_quota_service() -> FakeQuotaService:
    return FakeQuotaService()


@pytest.fixture
def mock_gemini_adapter() -> MockGeminiAdapter:
    return MockGeminiAdapter()


@pytest.fixture
def sanitizer() -> PromptSanitizer:
    return PromptSanitizer()


@pytest.fixture
def study_plan_service(
    fake_plan_repo: FakeStudyPlanRepository,
    fake_calendar_service: FakeCalendarService,
    mock_gemini_adapter: MockGeminiAdapter,
    sanitizer: PromptSanitizer,
    fake_quota_service: FakeQuotaService,
    fake_usage_logger: FakeUsageLogger,
) -> StudyPlanService:
    return StudyPlanService(
        study_plan_repo=fake_plan_repo,
        calendar_service=fake_calendar_service,
        gemini_adapter=mock_gemini_adapter,  # type: ignore[arg-type]
        sanitizer=sanitizer,
        quota_service=fake_quota_service,
        usage_logger=fake_usage_logger,
    )


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_plan_repo: FakeStudyPlanRepository,
    fake_calendar_service: FakeCalendarService,
    fake_usage_logger: FakeUsageLogger,
    fake_quota_service: FakeQuotaService,
    mock_gemini_adapter: MockGeminiAdapter,
    sanitizer: PromptSanitizer,
) -> AsyncGenerator[AsyncClient, None]:
    """Provide AsyncClient with all dependencies mocked and overridden."""
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_study_plan_repo] = lambda: fake_plan_repo
    app.dependency_overrides[get_calendar_service] = lambda: fake_calendar_service
    app.dependency_overrides[get_ai_usage_logger] = lambda: fake_usage_logger
    app.dependency_overrides[get_quota_service] = lambda: fake_quota_service
    app.dependency_overrides[get_gemini_adapter] = lambda: mock_gemini_adapter
    app.dependency_overrides[get_prompt_sanitizer] = lambda: sanitizer

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_header(user_id: uuid.UUID) -> dict[str, str]:
    """Generate Bearer Authorization header for user UUID."""
    token = create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(minutes=60),
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Unit & Schema Constraint Tests
# ---------------------------------------------------------------------------


def test_study_plan_database_table_args_and_constraints() -> None:
    """Verify that SQLAlchemy models define the exact check constraints specified in PostgreSQL baseline schema."""
    # 1. StudyPlan constraints
    plan_table: Any = StudyPlan.__table__
    constraint_names = {c.name for c in plan_table.constraints}
    assert "ck_study_plans_date" in constraint_names
    assert "ck_study_plans_status" in constraint_names

    date_check = next(c for c in plan_table.constraints if c.name == "ck_study_plans_date")
    assert isinstance(date_check, CheckConstraint)
    assert "end_date >= start_date" in str(date_check.sqltext)

    status_check = next(c for c in plan_table.constraints if c.name == "ck_study_plans_status")
    assert isinstance(status_check, CheckConstraint)
    sql_text = str(status_check.sqltext)
    assert "draft" in sql_text
    assert "active" in sql_text
    assert "completed" in sql_text
    assert "archived" in sql_text

    # 2. StudyPlanItem constraints
    item_table: Any = StudyPlanItem.__table__
    item_constraint_names = {c.name for c in item_table.constraints}
    assert "ck_study_plan_items_time" in item_constraint_names
    assert "ck_study_plan_items_status" in item_constraint_names
    assert "ck_study_plan_items_accepted_at" in item_constraint_names
    assert "ck_study_plan_items_completed_at" in item_constraint_names
    assert "ck_study_plan_items_order" in item_constraint_names

    time_check = next(c for c in item_table.constraints if c.name == "ck_study_plan_items_time")
    assert isinstance(time_check, CheckConstraint)
    assert "scheduled_end > scheduled_start" in str(time_check.sqltext)

    item_status_check = next(c for c in item_table.constraints if c.name == "ck_study_plan_items_status")
    assert isinstance(item_status_check, CheckConstraint)
    item_sql = str(item_status_check.sqltext)
    assert "pending" in item_sql
    assert "accepted" in item_sql
    assert "in_progress" in item_sql
    assert "completed" in item_sql
    assert "dismissed" in item_sql

    accepted_check = next(c for c in item_table.constraints if c.name == "ck_study_plan_items_accepted_at")
    assert isinstance(accepted_check, CheckConstraint)
    assert "accepted_at IS NOT NULL" in str(accepted_check.sqltext)

    completed_check = next(c for c in item_table.constraints if c.name == "ck_study_plan_items_completed_at")
    assert isinstance(completed_check, CheckConstraint)
    assert "completed_at IS NOT NULL" in str(completed_check.sqltext)


@pytest.mark.asyncio
async def test_study_plan_repository_with_mock_session() -> None:
    """Verify StudyPlanRepository methods interact with SQLAlchemy AsyncSession correctly."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_session.delete = AsyncMock()

    user_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    item_id = uuid.uuid4()

    mock_plan = StudyPlan(
        id=plan_id,
        user_id=user_id,
        name="Calculus Prep",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 7),
        status="draft",
    )
    mock_plan.items = []

    mock_item = StudyPlanItem(
        id=item_id,
        study_plan_id=plan_id,
        title="Chapter 1 Review",
        scheduled_start=datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
        scheduled_end=datetime(2026, 11, 2, 10, 0, tzinfo=UTC),
        status="pending",
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_plan
    mock_session.execute.return_value = mock_result

    repo = StudyPlanRepository(session=mock_session)

    # 1. create_plan
    created = await repo.create_plan(
        user_id=user_id,
        name="Calculus Prep",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 7),
    )
    assert created.name == "Calculus Prep"
    assert mock_session.add.call_count >= 1
    assert mock_session.commit.call_count >= 1

    # 2. get_plan
    fetched = await repo.get_plan(user_id=user_id, plan_id=plan_id)
    assert fetched is not None
    assert fetched.id == plan_id

    # 3. delete_plan
    deleted = await repo.delete_plan(user_id=user_id, plan_id=plan_id)
    assert deleted is True
    assert mock_session.delete.call_count == 1

    # 4. update_item_status
    mock_result.scalar_one_or_none.return_value = mock_item
    updated_item = await repo.update_item_status(
        user_id=user_id,
        plan_id=plan_id,
        item_id=item_id,
        status="accepted",
    )
    assert updated_item is not None
    assert updated_item.status == "accepted"
    assert updated_item.accepted_at is not None


# ---------------------------------------------------------------------------
# Integration & Service Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ai_study_plan_generation_date_and_time_mapping(
    study_plan_service: StudyPlanService,
    fake_calendar_service: FakeCalendarService,
    mock_gemini_adapter: MockGeminiAdapter,
    fake_usage_logger: FakeUsageLogger,
) -> None:
    """Verify that generated plan items map offsets to exact UTC datetimes and conform to constraints."""
    user_id = uuid.uuid4()
    start_date = date(2026, 11, 1)
    end_date = date(2026, 11, 7)

    # Setup matching course
    course_id = uuid.uuid4()
    fake_calendar_service.mock_courses = [
        Course(
            id=course_id,
            user_id=user_id,
            name="Data Structures",
            course_code="CS201",
        )
    ]

    plan_dto = await study_plan_service.generate_ai_study_plan(
        user_id=user_id,
        name="Midterm Prep",
        start_date=start_date,
        end_date=end_date,
        target_goals="Master trees and graphs",
        plan_code="pro",
    )

    assert plan_dto.name == "Midterm Prep"
    assert plan_dto.start_date == start_date
    assert plan_dto.end_date == end_date
    assert plan_dto.generated_by_ai is True
    assert plan_dto.status == "draft"
    assert len(plan_dto.items) == 3

    # Check Item 1: day_offset = 1, start_time = "09:00", duration = 90 min
    it1 = plan_dto.items[0]
    assert it1.title == "Review Binary Search Trees & AVL"
    assert it1.scheduled_start == datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert it1.scheduled_end == datetime(2026, 11, 2, 10, 30, tzinfo=UTC)
    assert it1.status == "pending"
    assert it1.accepted_at is None
    assert it1.completed_at is None
    assert it1.course_id == course_id  # Mapped from course_name="Data Structures"

    # Check Item 2: day_offset = 3, start_time = "14:30", duration = 60 min
    it2 = plan_dto.items[1]
    assert it2.scheduled_start == datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
    assert it2.scheduled_end == datetime(2026, 11, 4, 15, 30, tzinfo=UTC)

    # Check Telemetry
    assert len(fake_usage_logger.logged_calls) == 1
    log = fake_usage_logger.logged_calls[0]
    assert log["feature"] == "study_plan"
    assert log["user_id"] == user_id
    assert log["prompt_tokens"] > 0
    assert log["completion_tokens"] > 0


@pytest.mark.asyncio
async def test_ai_study_plan_academic_calendar_awareness(
    study_plan_service: StudyPlanService,
    fake_calendar_service: FakeCalendarService,
    mock_gemini_adapter: MockGeminiAdapter,
) -> None:
    """Verify that academic commitments (exams, assignments, classes) are injected into the LLM prompt."""
    user_id = uuid.uuid4()
    start_date = date(2026, 11, 1)
    end_date = date(2026, 11, 7)

    course_id = uuid.uuid4()
    exam_event = CalendarEventDTO(
        id=uuid.uuid4(),
        event_type="exam",
        title="CS201 Midterm Examination",
        course_id=course_id,
        course_name="Data Structures",
        course_code="CS201",
        start_time=datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
        end_time=datetime(2026, 11, 6, 11, 0, tzinfo=UTC),
        location="Hall A",
        status=None,
        priority="high",
        description="Comprehensive midterm covering trees and graphs",
    )
    assignment_event = CalendarEventDTO(
        id=uuid.uuid4(),
        event_type="assignment",
        title="Homework 4 Submission",
        course_id=course_id,
        course_name="Data Structures",
        course_code="CS201",
        start_time=datetime(2026, 11, 4, 23, 59, tzinfo=UTC),
        end_time=None,
        location=None,
        status="pending",
        priority="medium",
        description="AVL balancing homework",
    )
    fake_calendar_service.mock_events = [exam_event, assignment_event]

    await study_plan_service.generate_ai_study_plan(
        user_id=user_id,
        name="Exam Intensive Plan",
        start_date=start_date,
        end_date=end_date,
    )

    # Verify that the LLM prompt received the untrusted student context containing the events
    last_prompt = mock_gemini_adapter.last_prompt
    assert "CS201 Midterm Examination" in last_prompt
    assert "Homework 4 Submission" in last_prompt
    assert "Data Structures" in last_prompt
    assert "<untrusted_student_context>" in last_prompt
    assert "<user_query>" in last_prompt


@pytest.mark.asyncio
async def test_invalid_plan_date_range_rejection(
    study_plan_service: StudyPlanService,
) -> None:
    """Verify that start_date > end_date raises InvalidPlanDateError."""
    user_id = uuid.uuid4()
    with pytest.raises(InvalidPlanDateError) as exc_info:
        await study_plan_service.generate_ai_study_plan(
            user_id=user_id,
            name="Invalid Plan",
            start_date=date(2026, 11, 10),
            end_date=date(2026, 11, 5),
        )
    assert "cannot be earlier than start_date" in str(exc_info.value)


# ---------------------------------------------------------------------------
# API Endpoints & Authorization Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_generate_study_plan_endpoint_success(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
) -> None:
    """Verify POST /api/v1/study-plans/generate returns 201 with standardized envelope."""
    user = await fake_user_repo.create_user(
        email="student@edusense.edu",
        password_hash="argon2id$mockhash",
        full_name="Alice Student",
    )
    headers = make_auth_header(user.id)

    payload = {
        "name": "Midterm Revision",
        "start_date": "2026-11-01",
        "end_date": "2026-11-07",
        "target_goals": "Focus on data structures and calculus algorithms",
    }

    res = await client.post(
        "/api/v1/study-plans/generate",
        json=payload,
        headers=headers,
    )
    assert res.status_code == 201
    body = res.json()
    assert body["success"] is True
    assert body["message"] == "Study plan generated successfully."
    data = body["data"]
    assert data["name"] == "Midterm Revision"
    assert data["start_date"] == "2026-11-01"
    assert data["end_date"] == "2026-11-07"
    assert data["generated_by_ai"] is True
    assert len(data["items"]) == 3


@pytest.mark.asyncio
async def test_api_invalid_date_range_returns_422(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
) -> None:
    """Verify POST /api/v1/study-plans/generate returns 422 when end_date < start_date."""
    user = await fake_user_repo.create_user(
        email="student@edusense.edu",
        password_hash="argon2id$mockhash",
        full_name="Bob Student",
    )
    headers = make_auth_header(user.id)

    payload = {
        "start_date": "2026-11-10",
        "end_date": "2026-11-05",
    }
    res = await client.post(
        "/api/v1/study-plans/generate",
        json=payload,
        headers=headers,
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_api_strict_tenant_isolation(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
) -> None:
    """Verify that User B cannot view, delete, or update User A's study plans or items."""
    user_a = await fake_user_repo.create_user(
        email="user_a@edusense.edu",
        password_hash="argon2id$mock",
        full_name="User A",
    )
    user_b = await fake_user_repo.create_user(
        email="user_b@edusense.edu",
        password_hash="argon2id$mock",
        full_name="User B",
    )
    headers_a = make_auth_header(user_a.id)
    headers_b = make_auth_header(user_b.id)

    # 1. User A generates a plan
    res_a = await client.post(
        "/api/v1/study-plans/generate",
        json={"start_date": "2026-11-01", "end_date": "2026-11-07"},
        headers=headers_a,
    )
    assert res_a.status_code == 201
    plan_a_id = res_a.json()["data"]["id"]
    item_a_id = res_a.json()["data"]["items"][0]["id"]

    # 2. User B attempts to GET User A's plan -> 404
    get_res = await client.get(f"/api/v1/study-plans/{plan_a_id}", headers=headers_b)
    assert get_res.status_code == 404

    # 3. User B attempts to DELETE User A's plan -> 404
    del_res = await client.delete(f"/api/v1/study-plans/{plan_a_id}", headers=headers_b)
    assert del_res.status_code == 404

    # 4. User B attempts to update item status -> 404
    patch_res = await client.patch(
        f"/api/v1/study-plans/{plan_a_id}/items/{item_a_id}",
        json={"status": "accepted"},
        headers=headers_b,
    )
    assert patch_res.status_code == 404

    # 5. User B lists plans -> empty
    list_res = await client.get("/api/v1/study-plans", headers=headers_b)
    assert list_res.status_code == 200
    assert len(list_res.json()["data"]) == 0
    assert list_res.json()["meta"]["total"] == 0


@pytest.mark.asyncio
async def test_api_quota_exhaustion_returns_429(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_quota_service: FakeQuotaService,
) -> None:
    """Verify that exhausted AI daily quota returns HTTP 429 Too Many Requests."""
    user = await fake_user_repo.create_user(
        email="quota@edusense.edu",
        password_hash="argon2id$mock",
        full_name="Quota Student",
    )
    headers = make_auth_header(user.id)
    fake_quota_service.is_exhausted = True

    res = await client.post(
        "/api/v1/study-plans/generate",
        json={"start_date": "2026-11-01", "end_date": "2026-11-07"},
        headers=headers,
    )
    assert res.status_code == 429
    assert "quota" in res.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_quota_fail_closed_redis_down_returns_503(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    fake_quota_service: FakeQuotaService,
) -> None:
    """Verify that when Redis is down, system fails closed with HTTP 503 Service Unavailable."""
    user = await fake_user_repo.create_user(
        email="redisdown@edusense.edu",
        password_hash="argon2id$mock",
        full_name="Redis Student",
    )
    headers = make_auth_header(user.id)
    fake_quota_service.is_redis_down = True

    res = await client.post(
        "/api/v1/study-plans/generate",
        json={"start_date": "2026-11-01", "end_date": "2026-11-07"},
        headers=headers,
    )
    assert res.status_code == 503
    assert "fail-closed" in res.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_gemini_error_handling(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    mock_gemini_adapter: MockGeminiAdapter,
) -> None:
    """Verify proper HTTP status codes for Gemini rate limits (429), circuit breaker (503), and timeouts (504)."""
    user = await fake_user_repo.create_user(
        email="gemini_err@edusense.edu",
        password_hash="argon2id$mock",
        full_name="Error Student",
    )
    headers = make_auth_header(user.id)

    # 1. Rate Limit -> 429
    mock_gemini_adapter.simulate_rate_limit = True
    res_429 = await client.post(
        "/api/v1/study-plans/generate",
        json={"start_date": "2026-11-01", "end_date": "2026-11-07"},
        headers=headers,
    )
    assert res_429.status_code == 429
    mock_gemini_adapter.simulate_rate_limit = False

    # 2. Circuit Breaker Open -> 503
    mock_gemini_adapter.simulate_circuit_open = True
    res_503 = await client.post(
        "/api/v1/study-plans/generate",
        json={"start_date": "2026-11-01", "end_date": "2026-11-07"},
        headers=headers,
    )
    assert res_503.status_code == 503
    mock_gemini_adapter.simulate_circuit_open = False

    # 3. Timeout -> 504
    mock_gemini_adapter.simulate_timeout = True
    res_504 = await client.post(
        "/api/v1/study-plans/generate",
        json={"start_date": "2026-11-01", "end_date": "2026-11-07"},
        headers=headers,
    )
    assert res_504.status_code == 504
    mock_gemini_adapter.simulate_timeout = False


@pytest.mark.asyncio
async def test_api_manual_study_plan_crud_lifecycle(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
) -> None:
    """Verify full CRUD lifecycle: manual create, list, get, update item status, and delete."""
    user = await fake_user_repo.create_user(
        email="crud@edusense.edu",
        password_hash="argon2id$mock",
        full_name="Crud Student",
    )
    headers = make_auth_header(user.id)

    # 1. Create manually
    create_res = await client.post(
        "/api/v1/study-plans",
        json={
            "name": "Semester Final Revision",
            "start_date": "2026-12-01",
            "end_date": "2026-12-15",
            "description": "Manual revision plan",
        },
        headers=headers,
    )
    assert create_res.status_code == 201
    plan_id = create_res.json()["data"]["id"]

    # 2. List
    list_res = await client.get("/api/v1/study-plans", headers=headers)
    assert list_res.status_code == 200
    assert list_res.json()["meta"]["total"] == 1

    # 3. Get Details
    get_res = await client.get(f"/api/v1/study-plans/{plan_id}", headers=headers)
    assert get_res.status_code == 200
    assert get_res.json()["data"]["name"] == "Semester Final Revision"

    # 4. Generate AI plan and update item status
    ai_res = await client.post(
        "/api/v1/study-plans/generate",
        json={"start_date": "2026-11-01", "end_date": "2026-11-07"},
        headers=headers,
    )
    ai_plan_id = ai_res.json()["data"]["id"]
    item_id = ai_res.json()["data"]["items"][0]["id"]

    # Patch status to accepted (auto sets accepted_at)
    patch_accepted = await client.patch(
        f"/api/v1/study-plans/{ai_plan_id}/items/{item_id}",
        json={"status": "accepted"},
        headers=headers,
    )
    assert patch_accepted.status_code == 200
    assert patch_accepted.json()["data"]["status"] == "accepted"
    assert patch_accepted.json()["data"]["accepted_at"] is not None

    # Patch status to completed (auto sets completed_at)
    patch_completed = await client.patch(
        f"/api/v1/study-plans/{ai_plan_id}/items/{item_id}",
        json={"status": "completed"},
        headers=headers,
    )
    assert patch_completed.status_code == 200
    assert patch_completed.json()["data"]["status"] == "completed"
    assert patch_completed.json()["data"]["completed_at"] is not None

    # 5. Delete plan
    del_res = await client.delete(f"/api/v1/study-plans/{plan_id}", headers=headers)
    assert del_res.status_code == 200

    # 6. Verify deleted
    verify_res = await client.get(f"/api/v1/study-plans/{plan_id}", headers=headers)
    assert verify_res.status_code == 404


@pytest.mark.asyncio
async def test_api_unauthorized_access_rejected(
    client: AsyncClient,
) -> None:
    """Verify that unauthenticated requests to protected endpoints return 401 Unauthorized."""
    res_gen = await client.post(
        "/api/v1/study-plans/generate",
        json={"start_date": "2026-11-01", "end_date": "2026-11-07"},
    )
    assert res_gen.status_code == 401

    res_list = await client.get("/api/v1/study-plans")
    assert res_list.status_code == 401
