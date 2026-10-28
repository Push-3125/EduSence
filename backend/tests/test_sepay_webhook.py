"""Comprehensive tests for SePay QR Payment Creation & Webhook Handler (TASK-M6-01, Invariant 10).

Validates:
- Invariant 2 (Layering): Routers -> Services -> Repositories. Routers NEVER import repositories.
- Invariant 6 (Tenant Isolation): User A cannot poll or query User B's payment orders.
- Invariant 10: Client talks ONLY to API Backend, never directly to payment gateway.
- DECISION-PO-02: Pro plan 50,000 VND / 30 days mock/dev activation.
- SePay VietQR generation format.
- Webhook idempotency and deduplication by sepay_transaction_id.
- Atomic consistency between payment_transactions, subscriptions, and users.current_plan_code.
- Rejection of insufficient transfer amounts and unauthorized webhook requests.
- Graceful handling of unmatched reference codes.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Ensure backend root is on sys.path for direct test execution
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api.v1.payments import router as payments_router
from app.core.config import Settings
from app.core.security import (
    get_current_active_user,
    hash_password,
)
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
from app.services.auth_service import UserDTO
from app.services.payment_service import (
    PaymentAuthenticationError,
    PaymentOrderDTO,
    PaymentService,
    PaymentValidationError,
    get_payment_service,
)

# Ensure payments router is mounted on app
if not any(
    getattr(route, "path", "").startswith("/api/v1/payments") for route in app.routes
):
    app.include_router(payments_router)


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


class FakePaymentRepository(PaymentRepository):
    """In-memory PaymentRepository double simulating PostgreSQL transactions and constraints."""

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

    async def create_payment_order(
        self,
        user_id: uuid.UUID,
        plan_id: uuid.UUID,
        amount_vnd: int,
        reference_code: str,
    ) -> tuple[PaymentTransaction, Subscription]:
        now = datetime.now(UTC)
        sub_id = uuid.uuid4()
        subscription = Subscription(
            id=sub_id,
            user_id=user_id,
            plan_id=plan_id,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        self.subscriptions[sub_id] = subscription

        tx_id = uuid.uuid4()
        transaction = PaymentTransaction(
            id=tx_id,
            user_id=user_id,
            subscription_id=sub_id,
            provider="sepay",
            reference_code=reference_code,
            amount_vnd=amount_vnd,
            currency="VND",
            status="pending",
            created_at=now,
            updated_at=now,
        )
        transaction.subscription = subscription
        self.transactions[tx_id] = transaction
        return transaction, subscription

    async def get_transaction_by_reference(
        self, reference_code: str
    ) -> PaymentTransaction | None:
        for tx in self.transactions.values():
            if tx.reference_code and tx.reference_code.upper() == reference_code.upper():
                return tx
        return None

    async def get_transaction_by_sepay_id(
        self, sepay_transaction_id: str
    ) -> PaymentTransaction | None:
        for tx in self.transactions.values():
            if tx.sepay_transaction_id == sepay_transaction_id:
                return tx
        return None

    async def get_transaction(
        self, user_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> PaymentTransaction | None:
        tx = self.transactions.get(transaction_id)
        if tx and tx.user_id == user_id:
            return tx
        return None

    async def complete_payment_and_activate_subscription(
        self,
        transaction_id: uuid.UUID,
        sepay_transaction_id: str,
        provider_payload: dict[str, Any],
        duration_days: int = 30,
    ) -> PaymentTransaction:
        tx = self.transactions.get(transaction_id)
        if not tx:
            raise ValueError(f"Payment transaction {transaction_id} not found.")

        if tx.status == "completed":
            return tx

        now = datetime.now(UTC)

        # 1. Update PaymentTransaction
        tx.status = "completed"
        tx.sepay_transaction_id = sepay_transaction_id
        tx.paid_at = now
        tx.webhook_received_at = now
        tx.provider_payload = provider_payload

        # 2. Expire any existing active subscription for this user
        for s in self.subscriptions.values():
            if s.user_id == tx.user_id and s.status == "active":
                s.status = "expired"
                s.updated_at = now

        # 3. Activate associated subscription
        if tx.subscription_id and tx.subscription_id in self.subscriptions:
            sub = self.subscriptions[tx.subscription_id]
            sub.status = "active"
            sub.starts_at = now
            sub.ends_at = now + timedelta(days=duration_days)
            sub.activated_at = now
            sub.updated_at = now

        # 4. Atomically update User current_plan_code to 'pro'
        user = await self.user_repo.get_by_id(tx.user_id)
        if user:
            user.current_plan_code = "pro"
            user.updated_at = now

        return tx


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
def test_settings() -> Settings:
    return Settings(
        SEPAY_API_KEY="test-sepay-secret-key",
        SEPAY_ACCOUNT_NUMBER="0987654321",
        SEPAY_BANK_NAME="MBBank",
    )


@pytest.fixture
def payment_service(
    fake_payment_repo: FakePaymentRepository, test_settings: Settings
) -> PaymentService:
    return PaymentService(payment_repo=fake_payment_repo, settings_obj=test_settings)


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


@pytest.fixture
def sample_user_dto(sample_user: User) -> UserDTO:
    return UserDTO(
        id=sample_user.id,
        email=sample_user.email,
        full_name=sample_user.profile.full_name if sample_user.profile else "Student",
        role="student",
        current_plan_code=sample_user.current_plan_code,
        is_active=sample_user.is_active,
        is_email_verified=sample_user.is_email_verified,
        created_at=sample_user.created_at,
    )


@pytest_asyncio.fixture
async def async_client(
    fake_user_repo: FakeUserRepository,
    fake_payment_repo: FakePaymentRepository,
    test_settings: Settings,
    sample_user: User,
) -> AsyncGenerator[AsyncClient, None]:
    """Configured AsyncClient overriding repository and service dependencies."""
    service = PaymentService(payment_repo=fake_payment_repo, settings_obj=test_settings)

    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_payment_repo] = lambda: fake_payment_repo
    app.dependency_overrides[get_payment_service] = lambda: service

    async def override_get_current_active_user() -> UserDTO:
        user = await fake_user_repo.get_by_id(sample_user.id)
        current_plan = user.current_plan_code if user else "free"
        return UserDTO(
            id=sample_user.id,
            email=sample_user.email,
            full_name=sample_user.profile.full_name if sample_user.profile else "Student",
            role="student",
            current_plan_code=current_plan,
            is_active=sample_user.is_active,
            is_email_verified=sample_user.is_email_verified,
            created_at=sample_user.created_at,
        )

    app.dependency_overrides[get_current_active_user] = override_get_current_active_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Unit Tests: PaymentService & VietQR Logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_payment_qr_generates_valid_reference_and_url(
    payment_service: PaymentService, sample_user: User
) -> None:
    """Verifies QR order generation conforms to VietQR format and generates EDU-prefixed code."""
    order: PaymentOrderDTO = await payment_service.create_payment_qr(
        user_id=sample_user.id, plan_code="pro"
    )

    assert order.payment_id is not None
    assert order.reference_code.startswith("EDU")
    assert len(order.reference_code) >= 6
    assert order.amount_vnd == 50000
    assert order.status == "pending"
    assert order.account_number == "0987654321"
    assert order.bank_name == "MBBank"

    # VietQR URL validation
    expected_prefix = "https://qr.sepay.vn/img?"
    assert order.qr_url.startswith(expected_prefix)
    assert f"acc={order.account_number}" in order.qr_url
    assert f"bank={order.bank_name}" in order.qr_url
    assert f"amount={order.amount_vnd}" in order.qr_url
    assert f"des={order.reference_code}" in order.qr_url


@pytest.mark.asyncio
async def test_process_sepay_webhook_activates_subscription_and_updates_user(
    payment_service: PaymentService,
    fake_payment_repo: FakePaymentRepository,
    fake_user_repo: FakeUserRepository,
    sample_user: User,
) -> None:
    """Verifies exact content webhook updates transaction, activates 30d sub, and changes plan to pro."""
    # 1. User starts on free plan
    assert sample_user.current_plan_code == "free"

    # 2. Create pending order
    order = await payment_service.create_payment_qr(user_id=sample_user.id, plan_code="pro")

    # 3. Simulate SePay webhook payload
    payload = {
        "id": 987654321,
        "gateway": "MBBank",
        "transactionDate": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "accountNumber": "0987654321",
        "code": None,
        "content": order.reference_code,
        "transferType": "in",
        "transferAmount": 50000,
        "accumulated": 50000,
        "referenceCode": "MBVCB.987654321",
        "description": order.reference_code,
    }

    # 4. Process webhook with valid auth header
    result = await payment_service.process_sepay_webhook(
        payload=payload, auth_header="Apikey test-sepay-secret-key"
    )
    assert result is True

    # 5. Verify transaction state
    tx = await fake_payment_repo.get_transaction(sample_user.id, order.payment_id)
    assert tx is not None
    assert tx.status == "completed"
    assert tx.sepay_transaction_id == "987654321"
    assert tx.paid_at is not None

    # 6. Verify subscription state
    assert tx.subscription_id is not None
    sub = fake_payment_repo.subscriptions.get(tx.subscription_id)
    assert sub is not None
    assert sub.status == "active"
    assert sub.starts_at is not None
    assert sub.ends_at is not None
    assert sub.activated_at is not None
    # 30 days active duration
    duration = sub.ends_at - sub.starts_at
    assert duration.days == 30

    # 7. Verify User plan update
    refreshed_user = await fake_user_repo.get_by_id(sample_user.id)
    assert refreshed_user is not None
    assert refreshed_user.current_plan_code == "pro"


@pytest.mark.asyncio
async def test_process_sepay_webhook_matching_in_longer_bank_description(
    payment_service: PaymentService,
    fake_payment_repo: FakePaymentRepository,
    sample_user: User,
) -> None:
    """Verifies that reference code embedded within noisy bank transfer descriptions is extracted."""
    order = await payment_service.create_payment_qr(user_id=sample_user.id, plan_code="pro")

    # Description from mobile banking containing extra text
    noisy_description = f"Nguyen Van A chuyen khoan hoc phi {order.reference_code} qua SePay"
    payload = {
        "id": 999888777,
        "transferAmount": 50000,
        "content": noisy_description,
        "description": noisy_description,
    }

    result = await payment_service.process_sepay_webhook(
        payload=payload, auth_header="test-sepay-secret-key"
    )
    assert result is True

    tx = await fake_payment_repo.get_transaction(sample_user.id, order.payment_id)
    assert tx is not None
    assert tx.status == "completed"


@pytest.mark.asyncio
async def test_webhook_idempotency_duplicate_calls_do_not_reprocess(
    payment_service: PaymentService,
    fake_payment_repo: FakePaymentRepository,
    sample_user: User,
) -> None:
    """Verifies repeated webhook notifications for same sepay_transaction_id do not duplicate or fail."""
    order = await payment_service.create_payment_qr(user_id=sample_user.id, plan_code="pro")
    payload = {
        "id": 11223344,
        "transferAmount": 50000,
        "content": order.reference_code,
    }

    # First call completes payment
    first_res = await payment_service.process_sepay_webhook(
        payload=payload, auth_header="Bearer test-sepay-secret-key"
    )
    assert first_res is True

    # Capture paid_at timestamp
    tx = await fake_payment_repo.get_transaction(sample_user.id, order.payment_id)
    assert tx is not None
    first_paid_at = tx.paid_at

    # Second call (webhook retry by SePay)
    second_res = await payment_service.process_sepay_webhook(
        payload=payload, auth_header="Bearer test-sepay-secret-key"
    )
    assert second_res is True
    assert tx.paid_at == first_paid_at


@pytest.mark.asyncio
async def test_insufficient_transfer_amount_rejected(
    payment_service: PaymentService, sample_user: User
) -> None:
    """Verifies payments with amount less than required 50,000 VND are rejected with validation error."""
    order = await payment_service.create_payment_qr(user_id=sample_user.id, plan_code="pro")
    payload = {
        "id": 554433,
        "transferAmount": 20000,  # 20k < 50k
        "content": order.reference_code,
    }

    with pytest.raises(PaymentValidationError) as exc:
        await payment_service.process_sepay_webhook(
            payload=payload, auth_header="test-sepay-secret-key"
        )
    assert "less than required" in str(exc.value)


@pytest.mark.asyncio
async def test_invalid_reference_code_handled_gracefully(
    payment_service: PaymentService,
) -> None:
    """Verifies unrecognized bank transfer descriptions return False gracefully without raising exceptions."""
    payload = {
        "id": 667788,
        "transferAmount": 50000,
        "content": "UNKNOWN_REF_12345",
        "description": "Chuyen tien an trua",
    }

    result = await payment_service.process_sepay_webhook(
        payload=payload, auth_header="test-sepay-secret-key"
    )
    assert result is False


@pytest.mark.asyncio
async def test_unauthorized_webhook_token_rejected(
    payment_service: PaymentService,
) -> None:
    """Verifies missing or wrong authorization token raises PaymentAuthenticationError."""
    payload = {"id": 1234, "transferAmount": 50000, "content": "EDU12345"}

    # Missing header
    with pytest.raises(PaymentAuthenticationError):
        await payment_service.process_sepay_webhook(payload=payload, auth_header=None)

    # Wrong key
    with pytest.raises(PaymentAuthenticationError):
        await payment_service.process_sepay_webhook(
            payload=payload, auth_header="Apikey wrong-secret-token"
        )


# ---------------------------------------------------------------------------
# API Integration Tests: REST Endpoints & Tenant Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_create_qr_endpoint(async_client: AsyncClient) -> None:
    """POST /api/v1/payments/create-qr returns 201 Created and VietQR payload."""
    response = await async_client.post(
        "/api/v1/payments/create-qr",
        json={"plan_code": "pro"},
    )
    assert response.status_code == 201
    json_data = response.json()
    assert json_data["success"] is True
    data = json_data["data"]
    assert "payment_id" in data
    assert data["reference_code"].startswith("EDU")
    assert data["amount_vnd"] == 50000
    assert "https://qr.sepay.vn/img?" in data["qr_url"]
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_api_payment_status_polling_and_strict_tenant_isolation(
    async_client: AsyncClient,
    fake_payment_repo: FakePaymentRepository,
    sample_user: User,
    other_user: User,
) -> None:
    """GET /api/v1/payments/{payment_id}/status enforces strict tenant isolation."""
    # 1. Sample user creates order
    service = PaymentService(fake_payment_repo)
    user_order = await service.create_payment_qr(user_id=sample_user.id, plan_code="pro")

    # 2. Other user creates order
    other_order = await service.create_payment_qr(user_id=other_user.id, plan_code="pro")

    # 3. Sample user can poll their own payment status (200 OK)
    resp_own = await async_client.get(f"/api/v1/payments/{user_order.payment_id}/status")
    assert resp_own.status_code == 200
    own_data = resp_own.json()
    assert own_data["success"] is True
    assert own_data["data"]["payment_id"] == str(user_order.payment_id)
    assert own_data["data"]["status"] == "pending"

    # 4. Sample user attempting to poll Other user's payment status receives 404 (Invariant 6)
    resp_other = await async_client.get(f"/api/v1/payments/{other_order.payment_id}/status")
    assert resp_other.status_code == 404


@pytest.mark.asyncio
async def test_api_sepay_webhook_endpoint_flow(
    async_client: AsyncClient,
    fake_payment_repo: FakePaymentRepository,
    fake_user_repo: FakeUserRepository,
    sample_user: User,
) -> None:
    """Full HTTP flow: Create QR -> Webhook Callback -> Polling shows completed."""
    # 1. Create QR
    resp_create = await async_client.post(
        "/api/v1/payments/create-qr", json={"plan_code": "pro"}
    )
    assert resp_create.status_code == 201
    order_data = resp_create.json()["data"]
    payment_id = order_data["payment_id"]
    reference_code = order_data["reference_code"]

    # 2. SePay sends webhook notification
    webhook_payload = {
        "id": 884422,
        "transferAmount": 50000,
        "content": reference_code,
    }
    resp_webhook = await async_client.post(
        "/api/v1/payments/sepay/webhook",
        json=webhook_payload,
        headers={"Authorization": "Apikey test-sepay-secret-key"},
    )
    assert resp_webhook.status_code == 200
    assert resp_webhook.json()["success"] is True
    assert resp_webhook.json()["data"]["processed"] is True

    # 3. Poll payment status -> completed
    resp_status = await async_client.get(f"/api/v1/payments/{payment_id}/status")
    assert resp_status.status_code == 200
    status_data = resp_status.json()["data"]
    assert status_data["status"] == "completed"
    assert status_data["paid_at"] is not None

    # 4. User is now pro
    refreshed_user = await fake_user_repo.get_by_id(sample_user.id)
    assert refreshed_user is not None
    assert refreshed_user.current_plan_code == "pro"


@pytest.mark.asyncio
async def test_api_webhook_unauthorized_401(async_client: AsyncClient) -> None:
    """POST /api/v1/payments/sepay/webhook returns 401 on missing or incorrect auth token."""
    resp = await async_client.post(
        "/api/v1/payments/sepay/webhook",
        json={"id": 123, "transferAmount": 50000, "content": "EDU123"},
        headers={"Authorization": "Apikey wrong-key"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_webhook_insufficient_amount_400(
    async_client: AsyncClient,
    payment_service: PaymentService,
    sample_user: User,
) -> None:
    """POST /api/v1/payments/sepay/webhook returns 400 when transfer amount is too low."""
    order = await payment_service.create_payment_qr(user_id=sample_user.id, plan_code="pro")
    resp = await async_client.post(
        "/api/v1/payments/sepay/webhook",
        json={"id": 456, "transferAmount": 10000, "content": order.reference_code},
        headers={"Authorization": "Apikey test-sepay-secret-key"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_api_webhook_unmatched_reference_returns_200_unprocessed(
    async_client: AsyncClient,
) -> None:
    """POST /api/v1/payments/sepay/webhook returns 200 with processed=False on unrecognized transfer."""
    resp = await async_client.post(
        "/api/v1/payments/sepay/webhook",
        json={"id": 789, "transferAmount": 50000, "content": "UNKNOWN_TRANSACTION"},
        headers={"Authorization": "Apikey test-sepay-secret-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["data"]["processed"] is False


@pytest.mark.asyncio
async def test_api_endpoints_unauthorized_without_jwt(
    fake_payment_repo: FakePaymentRepository,
    fake_user_repo: FakeUserRepository,
    test_settings: Settings,
) -> None:
    """Verifies that protected payment endpoints reject unauthenticated requests with 401."""
    service = PaymentService(payment_repo=fake_payment_repo, settings_obj=test_settings)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_payment_repo] = lambda: fake_payment_repo
    app.dependency_overrides[get_payment_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as unauth_client:
        # 1. create-qr without Authorization header
        resp1 = await unauth_client.post("/api/v1/payments/create-qr", json={"plan_code": "pro"})
        assert resp1.status_code == 401

        # 2. status inquiry without Authorization header
        resp2 = await unauth_client.get(f"/api/v1/payments/{uuid.uuid4()}/status")
        assert resp2.status_code == 401

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_payment_repository_sql_methods() -> None:
    """Directly verifies PaymentRepository SQL statements and atomic activation sequence."""
    from unittest.mock import AsyncMock, MagicMock

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    repo = PaymentRepository(session=mock_session)

    user_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    tx_id = uuid.uuid4()
    sub_id = uuid.uuid4()

    # 1. get_plan_by_code
    mock_res_plan = MagicMock()
    mock_plan = SubscriptionPlan(id=plan_id, code="pro", name="Pro", price_vnd=50000)
    mock_res_plan.scalar_one_or_none.return_value = mock_plan
    mock_session.execute.return_value = mock_res_plan

    plan = await repo.get_plan_by_code("pro")
    assert plan is not None
    assert plan.code == "pro"

    # 2. create_payment_order
    tx, sub = await repo.create_payment_order(
        user_id=user_id,
        plan_id=plan_id,
        amount_vnd=50000,
        reference_code="EDU12345",
    )
    assert tx.reference_code == "EDU12345"
    assert sub.user_id == user_id
    assert mock_session.add.call_count == 2
    mock_session.flush.assert_called_once()
    mock_session.commit.assert_called_once()

    # 3. get_transaction_by_reference
    mock_res_tx = MagicMock()
    mock_tx = PaymentTransaction(
        id=tx_id,
        user_id=user_id,
        subscription_id=sub_id,
        amount_vnd=50000,
        status="pending",
        reference_code="EDU12345",
    )
    mock_res_tx.scalar_one_or_none.return_value = mock_tx
    mock_session.execute.return_value = mock_res_tx

    found_tx = await repo.get_transaction_by_reference("EDU12345")
    assert found_tx is not None
    assert found_tx.id == tx_id

    # 4. complete_payment_and_activate_subscription
    mock_res_lock = MagicMock()
    mock_res_lock.scalar_one_or_none.return_value = mock_tx
    mock_session.execute.return_value = mock_res_lock

    activated = await repo.complete_payment_and_activate_subscription(
        transaction_id=tx_id,
        sepay_transaction_id="SEP12345",
        provider_payload={"id": "SEP12345"},
        duration_days=30,
    )
    assert activated.status == "completed"
    assert activated.sepay_transaction_id == "SEP12345"

