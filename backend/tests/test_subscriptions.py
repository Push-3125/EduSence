"""Comprehensive tests for Subscription Lifecycle & Atomic Plan Transition Service (TASK-M6-02).

Validates:
- Invariant 2: Routers -> Services -> Repositories. Routers NEVER import repositories or run raw SQL.
- Invariant 6: Strict Tenant Isolation. Every subscription query and mutation MUST filter by user_id.
- Invariant: users.current_plan_code and subscriptions must be kept consistent ATOMICALLY.
- Default Free plan returns Free plan details (is_active=False, days_remaining=0).
- Active Pro subscription returns Pro details and correct days_remaining.
- Subscription cancellation downgrades user.current_plan_code to 'free' and marks subscription cancelled.
- Calling cancel when no active subscription returns 404 / NoActiveSubscriptionError.
- Subscription history listing ordered by created_at DESC.
- Automatic downgrade of expired subscriptions.
- Strict tenant isolation: User A cannot view or cancel User B's subscription.
- Authentication guard (401 when unauthenticated).
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Ensure backend root is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api.v1.subscriptions import router as subscriptions_router
from app.core.security import create_access_token, hash_password
from app.main import app
from app.repositories.payment_repo import (
    PaymentRepository,
    PaymentTransaction,
    Subscription,
    SubscriptionPlan,
    get_payment_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.subscription_service import (
    NoActiveSubscriptionError,
    SubscriptionDetailDTO,
    SubscriptionNotFoundError,
    SubscriptionService,
    get_subscription_service,
)

# Ensure subscriptions router is mounted on app
if not any(
    getattr(route, "path", "").startswith("/api/v1/subscriptions") for route in app.routes
):
    app.include_router(subscriptions_router)


# ---------------------------------------------------------------------------
# Test Doubles: In-Memory Fake Repositories
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for authentication and plan updates."""

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
            email=email,
            password_hash=password_hash,
            is_active=True,
            is_email_verified=is_email_verified,
            current_plan_code=current_plan_code,
            created_at=now,
            updated_at=now,
        )
        user.roles = [role]
        profile = UserProfile(
            id=uuid.uuid4(),
            user_id=user_id,
            full_name=full_name,
            created_at=now,
            updated_at=now,
        )
        user.profile = profile
        self.users[user_id] = user
        return user

    async def update_user_plan(
        self,
        user_id: uuid.UUID,
        plan_code: str,
    ) -> bool:
        user = self.users.get(user_id)
        if not user:
            return False
        user.current_plan_code = plan_code
        user.updated_at = datetime.now(UTC)
        return True


class FakePaymentRepository(PaymentRepository):
    """In-memory PaymentRepository double simulating PostgreSQL subscriptions table."""

    def __init__(self, user_repo: FakeUserRepository | None = None) -> None:
        self.user_repo = user_repo or FakeUserRepository()
        self.plans: dict[str, SubscriptionPlan] = {}
        self.subscriptions: dict[uuid.UUID, Subscription] = {}
        self.transactions: dict[uuid.UUID, PaymentTransaction] = {}

        # Seed default plans
        now = datetime.now(UTC)
        self.plans["free"] = SubscriptionPlan(
            id=uuid.uuid4(),
            code="free",
            name="Free",
            price_vnd=0,
            duration_days=None,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        self.plans["pro"] = SubscriptionPlan(
            id=uuid.uuid4(),
            code="pro",
            name="Pro",
            price_vnd=50000,
            duration_days=30,
            is_active=True,
            created_at=now,
            updated_at=now,
        )

    async def get_plan_by_code(self, code: str) -> SubscriptionPlan | None:
        return self.plans.get(code)

    def add_subscription(
        self,
        user_id: uuid.UUID,
        plan_code: str = "pro",
        status: str = "active",
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> Subscription:
        now = datetime.now(UTC)
        plan = self.plans.get(plan_code)
        sub = Subscription(
            id=uuid.uuid4(),
            user_id=user_id,
            plan_id=plan.id if plan else uuid.uuid4(),
            status=status,
            starts_at=starts_at or now,
            ends_at=ends_at or (now + timedelta(days=30)),
            created_at=created_at or now,
            updated_at=now,
        )
        sub.plan = plan or self.plans["pro"]
        self.subscriptions[sub.id] = sub

        user = self.user_repo.users.get(user_id)
        if user and status == "active":
            user.current_plan_code = plan_code

        return sub

    async def get_active_subscription(
        self, user_id: uuid.UUID
    ) -> Subscription | None:
        candidates = [
            s
            for s in self.subscriptions.values()
            if s.user_id == user_id and s.status == "active"
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.created_at, reverse=True)
        return candidates[0]

    async def get_subscription_by_id(
        self, user_id: uuid.UUID, subscription_id: uuid.UUID
    ) -> Subscription | None:
        sub = self.subscriptions.get(subscription_id)
        if sub and sub.user_id == user_id:
            return sub
        return None

    async def list_subscriptions(
        self, user_id: uuid.UUID
    ) -> list[Subscription]:
        user_subs = [s for s in self.subscriptions.values() if s.user_id == user_id]
        user_subs.sort(key=lambda s: s.created_at, reverse=True)
        return user_subs

    async def cancel_active_subscription(
        self, user_id: uuid.UUID
    ) -> Subscription | None:
        sub = await self.get_active_subscription(user_id)
        if not sub:
            return None

        now = datetime.now(UTC)
        sub.status = "cancelled"
        sub.cancelled_at = now
        sub.updated_at = now

        user = await self.user_repo.get_by_id(user_id)
        if user:
            user.current_plan_code = "free"
            user.updated_at = now

        return sub

    async def expire_subscription_and_downgrade(
        self, user_id: uuid.UUID, subscription_id: uuid.UUID
    ) -> Subscription | None:
        sub = self.subscriptions.get(subscription_id)
        if not sub or sub.user_id != user_id:
            return None

        now = datetime.now(UTC)
        sub.status = "expired"
        sub.updated_at = now

        user = await self.user_repo.get_by_id(user_id)
        if user:
            user.current_plan_code = "free"
            user.updated_at = now

        return sub


def make_auth_header(user_id: uuid.UUID) -> dict[str, str]:
    """Generate Bearer Authorization header containing standard JWT access token."""
    token = create_access_token(subject=str(user_id))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Test Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_user_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def fake_payment_repo(fake_user_repo: FakeUserRepository) -> FakePaymentRepository:
    return FakePaymentRepository(user_repo=fake_user_repo)


@pytest.fixture
def subscription_service(
    fake_payment_repo: FakePaymentRepository, fake_user_repo: FakeUserRepository
) -> SubscriptionService:
    return SubscriptionService(
        payment_repo=fake_payment_repo, user_repo=fake_user_repo
    )


@pytest_asyncio.fixture
async def sample_user(fake_user_repo: FakeUserRepository) -> User:
    return await fake_user_repo.create_user(
        email="student@university.edu.vn",
        password_hash=hash_password("StudentPassword123!"),
        full_name="Nguyen Van A",
        current_plan_code="free",
    )


@pytest_asyncio.fixture
async def other_user(fake_user_repo: FakeUserRepository) -> User:
    return await fake_user_repo.create_user(
        email="other@university.edu.vn",
        password_hash=hash_password("OtherPassword123!"),
        full_name="Tran Thi B",
        current_plan_code="free",
    )


@pytest_asyncio.fixture
async def async_client(
    fake_user_repo: FakeUserRepository,
    fake_payment_repo: FakePaymentRepository,
) -> AsyncGenerator[AsyncClient, None]:
    """Test AsyncClient configured with in-memory test doubles."""
    service = SubscriptionService(
        payment_repo=fake_payment_repo, user_repo=fake_user_repo
    )

    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_payment_repo] = lambda: fake_payment_repo
    app.dependency_overrides[get_subscription_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Unit Tests: SubscriptionService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_current_subscription_free_plan_default(
    subscription_service: SubscriptionService, sample_user: User
) -> None:
    """Verifies that a user on default Free plan returns Free plan details (is_active=False, days_remaining=0)."""
    dto: SubscriptionDetailDTO = await subscription_service.get_current_subscription(
        user_id=sample_user.id
    )

    assert dto.id is None
    assert dto.plan_code == "free"
    assert dto.plan_name == "Free"
    assert dto.status == "active"
    assert dto.is_active is False
    assert dto.days_remaining == 0
    assert dto.starts_at is None
    assert dto.ends_at is None


@pytest.mark.asyncio
async def test_get_current_subscription_active_pro(
    subscription_service: SubscriptionService,
    fake_payment_repo: FakePaymentRepository,
    sample_user: User,
) -> None:
    """Verifies that an active Pro subscription returns Pro details and accurate days_remaining."""
    now = datetime.now(UTC)
    sub = fake_payment_repo.add_subscription(
        user_id=sample_user.id,
        plan_code="pro",
        status="active",
        starts_at=now - timedelta(days=10),
        ends_at=now + timedelta(days=20),
    )

    dto: SubscriptionDetailDTO = await subscription_service.get_current_subscription(
        user_id=sample_user.id
    )

    assert dto.id == sub.id
    assert dto.plan_code == "pro"
    assert dto.plan_name == "Pro"
    assert dto.status == "active"
    assert dto.is_active is True
    assert dto.days_remaining == 20
    assert dto.starts_at == sub.starts_at
    assert dto.ends_at == sub.ends_at


@pytest.mark.asyncio
async def test_get_current_subscription_expired_downgrades_atomically(
    subscription_service: SubscriptionService,
    fake_payment_repo: FakePaymentRepository,
    fake_user_repo: FakeUserRepository,
    sample_user: User,
) -> None:
    """Verifies that an expired active subscription is atomically marked expired and downgraded to Free."""
    now = datetime.now(UTC)
    sub = fake_payment_repo.add_subscription(
        user_id=sample_user.id,
        plan_code="pro",
        status="active",
        starts_at=now - timedelta(days=35),
        ends_at=now - timedelta(days=5),
    )

    # Initial state was set to 'pro' by add_subscription
    user_before = await fake_user_repo.get_by_id(sample_user.id)
    assert user_before is not None
    assert user_before.current_plan_code == "pro"

    # Query current subscription -> triggers automatic downgrade
    dto: SubscriptionDetailDTO = await subscription_service.get_current_subscription(
        user_id=sample_user.id
    )

    assert dto.id is None
    assert dto.plan_code == "free"
    assert dto.is_active is False
    assert dto.days_remaining == 0

    # Verify atomic update in storage
    assert sub.status == "expired"
    user_after = await fake_user_repo.get_by_id(sample_user.id)
    assert user_after is not None
    assert user_after.current_plan_code == "free"


@pytest.mark.asyncio
async def test_cancel_subscription_success_and_downgrades_plan(
    subscription_service: SubscriptionService,
    fake_payment_repo: FakePaymentRepository,
    fake_user_repo: FakeUserRepository,
    sample_user: User,
) -> None:
    """Verifies that cancel_subscription marks sub cancelled and atomically sets user.current_plan_code to 'free'."""
    now = datetime.now(UTC)
    sub = fake_payment_repo.add_subscription(
        user_id=sample_user.id,
        plan_code="pro",
        status="active",
        starts_at=now - timedelta(days=5),
        ends_at=now + timedelta(days=25),
    )

    user = await fake_user_repo.get_by_id(sample_user.id)
    assert user is not None
    assert user.current_plan_code == "pro"

    dto = await subscription_service.cancel_subscription(user_id=sample_user.id)

    assert dto.id == sub.id
    assert dto.plan_code == "pro"
    assert dto.status == "cancelled"
    assert dto.is_active is False
    assert dto.days_remaining == 0

    # Ensure repository state consistency
    assert sub.status == "cancelled"
    assert sub.cancelled_at is not None
    user_updated = await fake_user_repo.get_by_id(sample_user.id)
    assert user_updated is not None
    assert user_updated.current_plan_code == "free"


@pytest.mark.asyncio
async def test_cancel_subscription_when_no_active_raises_error(
    subscription_service: SubscriptionService, sample_user: User
) -> None:
    """Verifies that attempting to cancel when no active subscription exists raises NoActiveSubscriptionError."""
    with pytest.raises(NoActiveSubscriptionError, match="No active subscription"):
        await subscription_service.cancel_subscription(user_id=sample_user.id)


@pytest.mark.asyncio
async def test_cancel_subscription_when_already_expired_raises_error(
    subscription_service: SubscriptionService,
    fake_payment_repo: FakePaymentRepository,
    sample_user: User,
) -> None:
    """Verifies that attempting to cancel an expired active subscription raises NoActiveSubscriptionError."""
    now = datetime.now(UTC)
    fake_payment_repo.add_subscription(
        user_id=sample_user.id,
        plan_code="pro",
        status="active",
        starts_at=now - timedelta(days=32),
        ends_at=now - timedelta(days=2),
    )

    with pytest.raises(NoActiveSubscriptionError, match="expired"):
        await subscription_service.cancel_subscription(user_id=sample_user.id)


@pytest.mark.asyncio
async def test_list_subscription_history_ordered_desc(
    subscription_service: SubscriptionService,
    fake_payment_repo: FakePaymentRepository,
    sample_user: User,
) -> None:
    """Verifies that subscription history is returned ordered by created_at DESC."""
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 2, 1, tzinfo=UTC)
    t3 = datetime(2026, 3, 1, tzinfo=UTC)

    sub1 = fake_payment_repo.add_subscription(
        user_id=sample_user.id,
        plan_code="pro",
        status="expired",
        starts_at=t1,
        ends_at=t1 + timedelta(days=30),
        created_at=t1,
    )
    sub2 = fake_payment_repo.add_subscription(
        user_id=sample_user.id,
        plan_code="pro",
        status="cancelled",
        starts_at=t2,
        ends_at=t2 + timedelta(days=30),
        created_at=t2,
    )
    sub3 = fake_payment_repo.add_subscription(
        user_id=sample_user.id,
        plan_code="pro",
        status="active",
        starts_at=t3,
        ends_at=t3 + timedelta(days=30),
        created_at=t3,
    )

    history = await subscription_service.list_subscription_history(user_id=sample_user.id)

    assert len(history) == 3
    assert history[0].id == sub3.id
    assert history[1].id == sub2.id
    assert history[2].id == sub1.id


@pytest.mark.asyncio
async def test_downgrade_expired_subscription_not_found(
    subscription_service: SubscriptionService, sample_user: User
) -> None:
    """Verifies that downgrade_expired_subscription raises SubscriptionNotFoundError when ID is missing."""
    with pytest.raises(SubscriptionNotFoundError):
        await subscription_service.downgrade_expired_subscription(
            user_id=sample_user.id, subscription_id=uuid.uuid4()
        )


# ---------------------------------------------------------------------------
# API Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_get_current_subscription_free_user(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """GET /api/v1/subscriptions/current returns default Free plan for free student."""
    response = await async_client.get(
        "/api/v1/subscriptions/current",
        headers=make_auth_header(sample_user.id),
    )

    assert response.status_code == 200
    res_json = response.json()
    assert res_json["success"] is True
    assert res_json["message"] == "Current subscription retrieved successfully."

    data = res_json["data"]
    assert data["id"] is None
    assert data["plan_code"] == "free"
    assert data["plan_name"] == "Free"
    assert data["status"] == "active"
    assert data["is_active"] is False
    assert data["days_remaining"] == 0
    assert data["starts_at"] is None
    assert data["ends_at"] is None


@pytest.mark.asyncio
async def test_api_get_current_subscription_pro_user(
    async_client: AsyncClient,
    fake_payment_repo: FakePaymentRepository,
    sample_user: User,
) -> None:
    """GET /api/v1/subscriptions/current returns Pro subscription details and days_remaining."""
    now = datetime.now(UTC)
    sub = fake_payment_repo.add_subscription(
        user_id=sample_user.id,
        plan_code="pro",
        status="active",
        starts_at=now - timedelta(days=5),
        ends_at=now + timedelta(days=25),
    )

    response = await async_client.get(
        "/api/v1/subscriptions/current",
        headers=make_auth_header(sample_user.id),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == str(sub.id)
    assert data["plan_code"] == "pro"
    assert data["plan_name"] == "Pro"
    assert data["status"] == "active"
    assert data["is_active"] is True
    assert data["days_remaining"] == 25
    assert data["starts_at"] is not None
    assert data["ends_at"] is not None


@pytest.mark.asyncio
async def test_api_cancel_subscription_success(
    async_client: AsyncClient,
    fake_payment_repo: FakePaymentRepository,
    sample_user: User,
) -> None:
    """POST /api/v1/subscriptions/cancel cancels active sub and downgrades plan."""
    now = datetime.now(UTC)
    sub = fake_payment_repo.add_subscription(
        user_id=sample_user.id,
        plan_code="pro",
        status="active",
        starts_at=now - timedelta(days=2),
        ends_at=now + timedelta(days=28),
    )

    response = await async_client.post(
        "/api/v1/subscriptions/cancel",
        headers=make_auth_header(sample_user.id),
    )

    assert response.status_code == 200
    res_json = response.json()
    assert res_json["success"] is True
    data = res_json["data"]
    assert data["id"] == str(sub.id)
    assert data["plan_code"] == "pro"
    assert data["status"] == "cancelled"
    assert data["is_active"] is False
    assert data["days_remaining"] == 0

    # Verify subsequent GET /current returns Free plan
    cur_res = await async_client.get(
        "/api/v1/subscriptions/current",
        headers=make_auth_header(sample_user.id),
    )
    cur_data = cur_res.json()["data"]
    assert cur_data["plan_code"] == "free"
    assert cur_data["is_active"] is False


@pytest.mark.asyncio
async def test_api_cancel_subscription_when_no_active_returns_404(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """POST /api/v1/subscriptions/cancel returns 404 when no active subscription exists."""
    response = await async_client.post(
        "/api/v1/subscriptions/cancel",
        headers=make_auth_header(sample_user.id),
    )

    assert response.status_code == 404
    res_json = response.json()
    assert res_json["success"] is False
    assert res_json["error"]["code"] == "HTTP_404"
    assert "No active subscription" in res_json["error"]["message"]


@pytest.mark.asyncio
async def test_api_subscription_history_listing(
    async_client: AsyncClient,
    fake_payment_repo: FakePaymentRepository,
    sample_user: User,
) -> None:
    """GET /api/v1/subscriptions/history lists past subscriptions ordered newest first."""
    t1 = datetime(2026, 1, 10, tzinfo=UTC)
    t2 = datetime(2026, 2, 10, tzinfo=UTC)

    sub1 = fake_payment_repo.add_subscription(
        user_id=sample_user.id,
        plan_code="pro",
        status="expired",
        starts_at=t1,
        ends_at=t1 + timedelta(days=30),
        created_at=t1,
    )
    sub2 = fake_payment_repo.add_subscription(
        user_id=sample_user.id,
        plan_code="pro",
        status="active",
        starts_at=t2,
        ends_at=t2 + timedelta(days=30),
        created_at=t2,
    )

    response = await async_client.get(
        "/api/v1/subscriptions/history",
        headers=make_auth_header(sample_user.id),
    )

    assert response.status_code == 200
    res_json = response.json()
    assert res_json["success"] is True
    history = res_json["data"]
    assert len(history) == 2
    assert history[0]["id"] == str(sub2.id)
    assert history[1]["id"] == str(sub1.id)


@pytest.mark.asyncio
async def test_api_strict_tenant_isolation(
    async_client: AsyncClient,
    fake_payment_repo: FakePaymentRepository,
    sample_user: User,
    other_user: User,
) -> None:
    """Verifies strict tenant isolation: User A's subscriptions cannot be viewed or cancelled by User B."""
    now = datetime.now(UTC)
    sub_a = fake_payment_repo.add_subscription(
        user_id=sample_user.id,
        plan_code="pro",
        status="active",
        starts_at=now,
        ends_at=now + timedelta(days=30),
    )

    # 1. Other user (B) checks current plan -> must see their own Free plan, NOT user A's Pro plan
    b_cur_res = await async_client.get(
        "/api/v1/subscriptions/current",
        headers=make_auth_header(other_user.id),
    )
    assert b_cur_res.status_code == 200
    assert b_cur_res.json()["data"]["plan_code"] == "free"
    assert b_cur_res.json()["data"]["is_active"] is False

    # 2. Other user (B) checks history -> must be empty
    b_hist_res = await async_client.get(
        "/api/v1/subscriptions/history",
        headers=make_auth_header(other_user.id),
    )
    assert b_hist_res.status_code == 200
    assert len(b_hist_res.json()["data"]) == 0

    # 3. Other user (B) tries to cancel -> 404 because B has no active subscription
    b_cancel_res = await async_client.post(
        "/api/v1/subscriptions/cancel",
        headers=make_auth_header(other_user.id),
    )
    assert b_cancel_res.status_code == 404

    # 4. Verify User A's subscription was completely untouched and remains active
    a_cur_res = await async_client.get(
        "/api/v1/subscriptions/current",
        headers=make_auth_header(sample_user.id),
    )
    assert a_cur_res.status_code == 200
    assert a_cur_res.json()["data"]["id"] == str(sub_a.id)
    assert a_cur_res.json()["data"]["status"] == "active"
    assert a_cur_res.json()["data"]["is_active"] is True


@pytest.mark.asyncio
async def test_api_unauthenticated_guard(async_client: AsyncClient) -> None:
    """Verifies that all subscription endpoints reject unauthenticated access with 401."""
    cur_res = await async_client.get("/api/v1/subscriptions/current")
    assert cur_res.status_code == 401

    hist_res = await async_client.get("/api/v1/subscriptions/history")
    assert hist_res.status_code == 401

    cancel_res = await async_client.post("/api/v1/subscriptions/cancel")
    assert cancel_res.status_code == 401
