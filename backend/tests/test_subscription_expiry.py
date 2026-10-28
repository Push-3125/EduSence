"""Comprehensive test suite for Celery Beat Subscription Expiry Scanner & Auto-Downgrade Worker (TASK-M6-03).

Validates:
- Invariant 8: Every worker task passes through the Idempotency Guard before execution.
- Invariant 9: Periodic tasks run via Celery Beat; DB consistency maintained atomically.
- Future active subscription: remains 'active', plan remains 'pro', no notification.
- Past active subscription: status becomes 'expired', user plan downgraded to 'free', system notification created.
- Cancelled or already expired subscription: untouched.
- Idempotency guard: duplicate invocation with same run_key does not duplicate notifications.
- Batch handling: multiple users with expired subscriptions all properly downgraded.
- Beat schedule & task routing configurations.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from celery.schedules import crontab

# Ensure backend root is on sys.path for direct execution
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.repositories.notification_repo import Notification, NotificationRepository
from app.repositories.payment_repo import (
    PaymentRepository,
    Subscription,
    SubscriptionPlan,
)
from app.repositories.user_repo import User
from app.worker.celery_app import celery_app
from app.worker.idempotency import (
    IdempotencyGuard,
    set_idempotency_guard,
)
from app.worker.tasks.downgrade_task import (
    _extract_downgrade_scan_key,
    scan_and_downgrade_expired_subscriptions,
)

# ---------------------------------------------------------------------------
# Test Doubles & Mocks
# ---------------------------------------------------------------------------


class MockRedisClient:
    """In-memory mock Redis client supporting TTL and atomic SET NX operations."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expires_at: dict[str, float] = {}

    def set(
        self,
        name: str,
        value: Any,
        ex: float | None = None,
        nx: bool = False,
    ) -> bool:
        if nx and name in self.store:
            return False
        self.store[name] = str(value)
        return True

    def get(self, name: str) -> str | None:
        return self.store.get(name)

    def delete(self, *names: str) -> int:
        count = 0
        for name in names:
            if name in self.store:
                del self.store[name]
                count += 1
        return count


class FakeNotificationRepository(NotificationRepository):
    """In-memory notification repository double for worker tests."""

    def __init__(self) -> None:
        self.notifications: list[Notification] = []
        self.dedup_keys: set[tuple[uuid.UUID, str, str]] = set()

    async def create(
        self,
        user_id: uuid.UUID,
        notification_type: str,
        category: str,
        title: str,
        content: str,
        channel: str = "in_app",
        deduplication_key: str | None = None,
        target_url: str | None = None,
        scheduled_at: datetime | None = None,
        sent_at: datetime | None = None,
        status: str = "pending",
    ) -> Notification:
        notif = Notification(
            id=uuid.uuid4(),
            user_id=user_id,
            notification_type=notification_type,
            category=category,
            title=title,
            content=content,
            channel=channel,
            deduplication_key=deduplication_key,
            target_url=target_url,
            scheduled_at=scheduled_at,
            sent_at=sent_at,
            status=status,
            is_read=False,
            read_at=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.notifications.append(notif)
        if deduplication_key:
            self.dedup_keys.add((user_id, channel, deduplication_key))
        return notif

    async def exists_by_dedup_key(
        self,
        user_id: uuid.UUID,
        channel: str,
        deduplication_key: str,
    ) -> bool:
        return (user_id, channel, deduplication_key) in self.dedup_keys


class FakePaymentRepository(PaymentRepository):
    """In-memory PaymentRepository double for expiry scanner and downgrade tests."""

    def __init__(self) -> None:
        self.subscriptions: dict[uuid.UUID, Subscription] = {}
        self.users: dict[uuid.UUID, User] = {}
        self.plans: dict[str, SubscriptionPlan] = {
            "pro": SubscriptionPlan(
                id=uuid.uuid4(),
                code="pro",
                name="Pro Plan",
                price_vnd=50000,
                duration_days=30,
                is_active=True,
            ),
            "free": SubscriptionPlan(
                id=uuid.uuid4(),
                code="free",
                name="Free Plan",
                price_vnd=0,
                duration_days=0,
                is_active=True,
            ),
        }

    def add_user_with_subscription(
        self,
        email: str,
        current_plan_code: str = "pro",
        status: str = "active",
        ends_at: datetime | None = None,
    ) -> tuple[User, Subscription]:
        user_id = uuid.uuid4()
        user = User(
            id=user_id,
            email=email,
            password_hash="mock_hash",
            current_plan_code=current_plan_code,
            is_active=True,
        )
        self.users[user_id] = user

        now = datetime.now(UTC)
        sub_id = uuid.uuid4()
        sub = Subscription(
            id=sub_id,
            user_id=user_id,
            plan_id=self.plans["pro"].id,
            status=status,
            starts_at=now - timedelta(days=31),
            ends_at=ends_at,
            activated_at=now - timedelta(days=31),
            created_at=now - timedelta(days=31),
            updated_at=now - timedelta(days=31),
        )
        sub.user = user
        sub.plan = self.plans["pro"]
        self.subscriptions[sub_id] = sub
        return user, sub

    async def get_expired_active_subscriptions(
        self, limit: int = 100
    ) -> list[Subscription]:
        now = datetime.now(UTC)
        expired: list[Subscription] = []
        for sub in self.subscriptions.values():
            if sub.status == "active" and sub.ends_at is not None:
                ends = sub.ends_at if sub.ends_at.tzinfo else sub.ends_at.replace(tzinfo=UTC)
                if ends <= now:
                    expired.append(sub)
        # Sort by ends_at ascending and apply limit
        expired.sort(key=lambda s: s.ends_at or now)
        return expired[:limit]

    async def batch_downgrade_subscription(
        self, subscription_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        sub = self.subscriptions.get(subscription_id)
        if not sub or sub.user_id != user_id or sub.status != "active":
            return False

        now = datetime.now(UTC)
        sub.status = "expired"
        sub.updated_at = now

        user = self.users.get(user_id)
        if user:
            user.current_plan_code = "free"
            user.updated_at = now

        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_globals() -> Any:
    """Reset global idempotency guard with in-memory redis double before each test."""
    mock_redis = MockRedisClient()
    guard = IdempotencyGuard(redis_client=mock_redis)
    set_idempotency_guard(guard)

    yield

    set_idempotency_guard(None)


@pytest.fixture
def fake_payment_repo() -> FakePaymentRepository:
    return FakePaymentRepository()


@pytest.fixture
def fake_notif_repo() -> FakeNotificationRepository:
    return FakeNotificationRepository()


# ---------------------------------------------------------------------------
# Tests: Celery Beat Expiry Scanner & Auto-Downgrade Worker
# ---------------------------------------------------------------------------


def test_future_active_subscription_untouched(
    fake_payment_repo: FakePaymentRepository,
    fake_notif_repo: FakeNotificationRepository,
) -> None:
    """Future active subscription remains 'active', plan remains 'pro', no notification created."""
    now = datetime.now(UTC)
    user, sub = fake_payment_repo.add_user_with_subscription(
        email="future_student@test.edu.vn",
        current_plan_code="pro",
        status="active",
        ends_at=now + timedelta(days=15),
    )

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        yield AsyncMock()

    with patch("app.worker.tasks.downgrade_task.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.downgrade_task.PaymentRepository", return_value=fake_payment_repo), \
         patch("app.worker.tasks.downgrade_task.NotificationRepository", return_value=fake_notif_repo):

        result = scan_and_downgrade_expired_subscriptions()

    assert result["scanned"] == 0
    assert result["downgraded"] == 0
    assert result["processed_ids"] == []

    # Verification: Subscription remains active, user remains pro, no notifications
    assert sub.status == "active"
    assert user.current_plan_code == "pro"
    assert len(fake_notif_repo.notifications) == 0


def test_past_active_subscription_downgraded_and_notified(
    fake_payment_repo: FakePaymentRepository,
    fake_notif_repo: FakeNotificationRepository,
) -> None:
    """Past active subscription status becomes 'expired', user downgraded to 'free', notification created."""
    now = datetime.now(UTC)
    user, sub = fake_payment_repo.add_user_with_subscription(
        email="expired_student@test.edu.vn",
        current_plan_code="pro",
        status="active",
        ends_at=now - timedelta(hours=2),
    )

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        yield AsyncMock()

    with patch("app.worker.tasks.downgrade_task.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.downgrade_task.PaymentRepository", return_value=fake_payment_repo), \
         patch("app.worker.tasks.downgrade_task.NotificationRepository", return_value=fake_notif_repo):

        result = scan_and_downgrade_expired_subscriptions()

    assert result["scanned"] == 1
    assert result["downgraded"] == 1
    assert str(sub.id) in result["processed_ids"]

    # Verification: Subscription expired and User plan downgraded
    assert sub.status == "expired"
    assert user.current_plan_code == "free"

    # Verification: Notification created with exact required payload
    assert len(fake_notif_repo.notifications) == 1
    notif = fake_notif_repo.notifications[0]
    assert notif.user_id == user.id
    assert notif.title == "Gói Pro đã hết hạn"
    assert notif.content == "Gói Pro của bạn đã hết hạn. Tài khoản đã được chuyển về gói Miễn phí (Free)."
    assert notif.notification_type == "system"
    assert notif.channel == "in_app"
    assert notif.status == "sent"


def test_cancelled_or_already_expired_subscription_untouched(
    fake_payment_repo: FakePaymentRepository,
    fake_notif_repo: FakeNotificationRepository,
) -> None:
    """Cancelled or already expired subscriptions in the past are untouched."""
    now = datetime.now(UTC)
    _user1, sub1 = fake_payment_repo.add_user_with_subscription(
        email="cancelled@test.edu.vn",
        current_plan_code="free",
        status="cancelled",
        ends_at=now - timedelta(days=5),
    )
    _user2, sub2 = fake_payment_repo.add_user_with_subscription(
        email="already_expired@test.edu.vn",
        current_plan_code="free",
        status="expired",
        ends_at=now - timedelta(days=2),
    )

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        yield AsyncMock()

    with patch("app.worker.tasks.downgrade_task.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.downgrade_task.PaymentRepository", return_value=fake_payment_repo), \
         patch("app.worker.tasks.downgrade_task.NotificationRepository", return_value=fake_notif_repo):

        result = scan_and_downgrade_expired_subscriptions()

    assert result["scanned"] == 0
    assert result["downgraded"] == 0
    assert result["processed_ids"] == []

    assert sub1.status == "cancelled"
    assert sub2.status == "expired"
    assert len(fake_notif_repo.notifications) == 0


def test_idempotency_guard_prevents_duplicate_notifications(
    fake_payment_repo: FakePaymentRepository,
    fake_notif_repo: FakeNotificationRepository,
) -> None:
    """Duplicate task execution with same run_key is intercepted by IdempotencyGuard."""
    now = datetime.now(UTC)
    _user, _sub = fake_payment_repo.add_user_with_subscription(
        email="idempotent_student@test.edu.vn",
        current_plan_code="pro",
        status="active",
        ends_at=now - timedelta(minutes=30),
    )

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        yield AsyncMock()

    task_key = "cron_run_test_downgrade_001"

    with patch("app.worker.tasks.downgrade_task.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.downgrade_task.PaymentRepository", return_value=fake_payment_repo), \
         patch("app.worker.tasks.downgrade_task.NotificationRepository", return_value=fake_notif_repo):

        # First execution succeeds
        result1 = scan_and_downgrade_expired_subscriptions(run_key=task_key)
        assert result1 is not None
        assert result1["scanned"] == 1
        assert result1["downgraded"] == 1
        assert len(fake_notif_repo.notifications) == 1

        # Second execution with identical run_key is intercepted
        result2 = scan_and_downgrade_expired_subscriptions(run_key=task_key)
        assert result2 is None  # Skipped by @idempotent_task

        # Ensure no additional notification was created
        assert len(fake_notif_repo.notifications) == 1


def test_batch_handling_multiple_users(
    fake_payment_repo: FakePaymentRepository,
    fake_notif_repo: FakeNotificationRepository,
) -> None:
    """Batch of multiple users with expired subscriptions are all processed and downgraded atomically."""
    now = datetime.now(UTC)
    users_and_subs = [
        fake_payment_repo.add_user_with_subscription(
            email=f"student_{i}@test.edu.vn",
            current_plan_code="pro",
            status="active",
            ends_at=now - timedelta(hours=i + 1),
        )
        for i in range(5)
    ]

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        yield AsyncMock()

    with patch("app.worker.tasks.downgrade_task.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.downgrade_task.PaymentRepository", return_value=fake_payment_repo), \
         patch("app.worker.tasks.downgrade_task.NotificationRepository", return_value=fake_notif_repo):

        result = scan_and_downgrade_expired_subscriptions()

    assert result["scanned"] == 5
    assert result["downgraded"] == 5
    assert len(result["processed_ids"]) == 5

    # Verify all users are downgraded to 'free' and all subscriptions are 'expired'
    for user, sub in users_and_subs:
        assert sub.status == "expired"
        assert user.current_plan_code == "free"

    # Verify 5 distinct system notifications were created
    assert len(fake_notif_repo.notifications) == 5
    notif_user_ids = {n.user_id for n in fake_notif_repo.notifications}
    assert len(notif_user_ids) == 5


def test_batch_limit_respected(
    fake_payment_repo: FakePaymentRepository,
    fake_notif_repo: FakeNotificationRepository,
) -> None:
    """Batch scanning respects limit parameter."""
    now = datetime.now(UTC)
    for i in range(10):
        fake_payment_repo.add_user_with_subscription(
            email=f"batch_student_{i}@test.edu.vn",
            current_plan_code="pro",
            status="active",
            ends_at=now - timedelta(hours=i + 1),
        )

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        yield AsyncMock()

    with patch("app.worker.tasks.downgrade_task.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.downgrade_task.PaymentRepository", return_value=fake_payment_repo), \
         patch("app.worker.tasks.downgrade_task.NotificationRepository", return_value=fake_notif_repo):

        result = scan_and_downgrade_expired_subscriptions(limit=3)

    assert result["scanned"] == 3
    assert result["downgraded"] == 3
    assert len(result["processed_ids"]) == 3
    assert len(fake_notif_repo.notifications) == 3


@pytest.mark.asyncio
async def test_payment_repo_batch_downgrade_atomicity(
    fake_payment_repo: FakePaymentRepository,
) -> None:
    """Repository batch_downgrade_subscription handles invalid or non-active subscriptions gracefully."""
    now = datetime.now(UTC)
    user, sub = fake_payment_repo.add_user_with_subscription(
        email="atomicity_test@test.edu.vn",
        current_plan_code="pro",
        status="active",
        ends_at=now - timedelta(days=1),
    )

    # 1. Non-existent subscription ID returns False
    res = await fake_payment_repo.batch_downgrade_subscription(uuid.uuid4(), user.id)
    assert res is False

    # 2. Mismatched user_id returns False
    res = await fake_payment_repo.batch_downgrade_subscription(sub.id, uuid.uuid4())
    assert res is False

    # 3. Valid downgrade succeeds
    res = await fake_payment_repo.batch_downgrade_subscription(sub.id, user.id)
    assert res is True
    assert sub.status == "expired"
    assert user.current_plan_code == "free"

    # 4. Attempting to downgrade again when already expired returns False
    res2 = await fake_payment_repo.batch_downgrade_subscription(sub.id, user.id)
    assert res2 is False


def test_extract_downgrade_scan_key() -> None:
    """Verify task idempotency key extraction handles kwargs, args, and defaults."""
    # From kwargs
    assert _extract_downgrade_scan_key(run_key="custom_run_1") == "custom_run_1"

    # From positional args
    assert _extract_downgrade_scan_key("test_arg_scan") == "test_arg_scan"
    assert _extract_downgrade_scan_key(None, "run_hourly_123") == "run_hourly_123"

    # Generated default
    key = _extract_downgrade_scan_key()
    assert key.startswith("downgrade_scan_")


def test_celery_beat_schedule_and_task_routes() -> None:
    """Verify Celery Beat schedule and task queue routing adhering to Invariant 9."""
    # Task route
    routes = celery_app.conf.task_routes
    assert "app.worker.tasks.downgrade_task.*" in routes
    assert routes["app.worker.tasks.downgrade_task.*"]["queue"] == "subscriptions"

    # Beat schedule
    schedule = celery_app.conf.beat_schedule
    assert "scan-and-downgrade-expired-subscriptions-hourly" in schedule

    entry = schedule["scan-and-downgrade-expired-subscriptions-hourly"]
    assert (
        entry["task"]
        == "app.worker.tasks.downgrade_task.scan_and_downgrade_expired_subscriptions"
    )
    assert entry["schedule"] == crontab(minute=30)


@pytest.mark.asyncio
async def test_real_payment_repo_get_expired_active_subscriptions_query_structure() -> None:
    """Test PaymentRepository.get_expired_active_subscriptions builds valid SQLAlchemy query."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    repo = PaymentRepository(session=mock_session)
    res = await repo.get_expired_active_subscriptions(limit=25)

    assert res == []
    assert mock_session.execute.called
    stmt = mock_session.execute.call_args[0][0]
    # Check that query targets Subscription table
    compiled = str(stmt)
    assert "subscriptions" in compiled
    assert "status =" in compiled


@pytest.mark.asyncio
async def test_real_payment_repo_batch_downgrade_subscription_success() -> None:
    """Test PaymentRepository.batch_downgrade_subscription successfully commits update."""
    now = datetime.now(UTC)
    sub_id = uuid.uuid4()
    user_id = uuid.uuid4()
    mock_sub = Subscription(
        id=sub_id,
        user_id=user_id,
        status="active",
        starts_at=now - timedelta(days=30),
        ends_at=now - timedelta(minutes=10),
    )

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_sub
    mock_session.execute.return_value = mock_result

    repo = PaymentRepository(session=mock_session)
    success = await repo.batch_downgrade_subscription(sub_id, user_id)

    assert success is True
    assert mock_sub.status == "expired"
    assert mock_session.commit.called


@pytest.mark.asyncio
async def test_real_payment_repo_batch_downgrade_subscription_not_found() -> None:
    """Test PaymentRepository.batch_downgrade_subscription returns False when not found."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    repo = PaymentRepository(session=mock_session)
    success = await repo.batch_downgrade_subscription(uuid.uuid4(), uuid.uuid4())

    assert success is False
    assert not mock_session.commit.called

