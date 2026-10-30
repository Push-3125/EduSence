"""Comprehensive test suite for Admin Student Accounts Management (US 2.22 / TASK-M6-04)."""

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# Ensure backend root is on sys.path for direct execution
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.admin.users import router as admin_users_router
from app.core.security import create_access_token, hash_password
from app.main import app
from app.repositories.audit_repo import (
    AuditRepository,
    AuditSecurityEvent,
    get_audit_repo,
)
from app.repositories.payment_repo import (
    PaymentRepository,
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
from app.services.admin_service import (
    AdminActionError,
    AdminUserNotFoundError,
    AdminUserService,
    get_admin_user_service,
)
from app.services.auth_service import UserDTO

# Ensure admin_users router is mounted on app
if not any(getattr(route, "path", "").startswith("/api/v1/admin/users") for route in app.routes):
    app.include_router(admin_users_router)


# ---------------------------------------------------------------------------
# Test Doubles
# ---------------------------------------------------------------------------


class FakeAuditRepository(AuditRepository):
    """In-memory AuditRepository double tracking security events."""

    def __init__(self) -> None:
        self.events: list[AuditSecurityEvent] = []

    async def log_security_event(
        self,
        event_type: str,
        actor_user_id: uuid.UUID | None = None,
        target_user_id: uuid.UUID | None = None,
        severity: str = "info",
        ip_address: str | None = None,
        user_agent: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditSecurityEvent:
        event = AuditSecurityEvent(
            id=uuid.uuid4(),
            event_type=event_type,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            severity=severity,
            ip_address=ip_address,
            user_agent=user_agent,
            details=details,
            created_at=datetime.now(UTC),
        )
        self.events.append(event)
        return event


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for admin student testing."""

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
            "admin": Role(
                id=uuid.uuid4(),
                name="admin",
                description="Administrator role.",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        }
        self.revoked_user_ids: list[uuid.UUID] = []

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self.users.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        norm = email.lower().strip()
        for u in self.users.values():
            if u.email.lower().strip() == norm:
                return u
        return None

    async def get_role_by_name(self, name: str) -> Role | None:
        return self.roles.get(name)

    async def create_user(
        self,
        email: str,
        password_hash: str,
        full_name: str,
        role_name: str = "student",
        current_plan_code: str = "free",
        is_email_verified: bool = False,
        avatar_url: str | None = None,
        university: str | None = None,
        major: str | None = None,
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
            current_plan_code=current_plan_code,
            is_active=True,
            is_email_verified=is_email_verified,
            created_at=now,
            updated_at=now,
        )
        profile = UserProfile(
            id=uuid.uuid4(),
            user_id=user_id,
            full_name=full_name.strip(),
            avatar_url=avatar_url,
            university=university,
            major=major,
            created_at=now,
            updated_at=now,
        )
        user.profile = profile
        user.roles = [role]
        self.users[user_id] = user
        return user

    async def list_users(
        self,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
        is_active: bool | None = None,
        plan_code: str | None = None,
        role_name: str | None = None,
    ) -> tuple[list[User], int]:
        filtered = list(self.users.values())

        if role_name:
            norm_role = role_name.strip().lower()
            filtered = [
                u
                for u in filtered
                if any(r.name.lower() == norm_role for r in (u.roles or []))
            ]

        if search and search.strip():
            term = search.strip().lower()
            filtered = [
                u
                for u in filtered
                if term in u.email.lower()
                or (u.profile and term in u.profile.full_name.lower())
            ]

        if is_active is not None:
            filtered = [u for u in filtered if u.is_active == is_active]

        if plan_code and plan_code.strip():
            norm_plan = plan_code.strip().lower()
            filtered = [
                u for u in filtered if u.current_plan_code.lower() == norm_plan
            ]

        filtered.sort(key=lambda u: u.created_at, reverse=True)
        total_count = len(filtered)
        offset = max(0, (page - 1) * page_size)
        paged = filtered[offset : offset + page_size]
        return paged, total_count

    async def update_user_status(
        self, user_id: uuid.UUID, is_active: bool
    ) -> User | None:
        user = self.users.get(user_id)
        if not user:
            return None
        user.is_active = is_active
        user.updated_at = datetime.now(UTC)
        return user

    async def update_user_plan(self, user_id: uuid.UUID, plan_code: str) -> bool:
        user = self.users.get(user_id)
        if not user:
            return False
        user.current_plan_code = plan_code
        user.updated_at = datetime.now(UTC)
        return True

    async def revoke_all_user_sessions(
        self, user_id: uuid.UUID, revoked_at: datetime | None = None
    ) -> int:
        self.revoked_user_ids.append(user_id)
        return 1


class FakePaymentRepository(PaymentRepository):
    """In-memory PaymentRepository double for admin subscription overrides."""

    def __init__(self, user_repo: FakeUserRepository) -> None:
        self.user_repo = user_repo
        self.plans: dict[str, SubscriptionPlan] = {}
        self.subscriptions: dict[uuid.UUID, Subscription] = {}

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
        return self.plans.get(code.lower().strip())

    async def get_active_subscription(self, user_id: uuid.UUID) -> Subscription | None:
        now = datetime.now(UTC)
        for sub in self.subscriptions.values():
            if sub.user_id == user_id and sub.status == "active":
                ends_at = sub.ends_at
                if ends_at and ends_at.tzinfo is None:
                    ends_at = ends_at.replace(tzinfo=UTC)
                if ends_at is None or ends_at > now:
                    return sub
        return None

    async def override_user_subscription(
        self,
        user_id: uuid.UUID,
        plan: SubscriptionPlan,
        duration_days: int | None = None,
    ) -> Subscription | None:
        now = datetime.now(UTC)

        for existing_sub in self.subscriptions.values():
            if existing_sub.user_id == user_id and existing_sub.status == "active":
                existing_sub.status = "expired"
                existing_sub.updated_at = now

        new_sub: Subscription | None = None
        if plan.code.lower() != "free":
            effective_duration = (
                duration_days
                if duration_days is not None
                else (plan.duration_days or 30)
            )
            new_sub = Subscription(
                id=uuid.uuid4(),
                user_id=user_id,
                plan_id=plan.id,
                status="active",
                starts_at=now,
                ends_at=now + timedelta(days=effective_duration),
                activated_at=now,
                created_at=now,
                updated_at=now,
            )
            new_sub.plan = plan
            self.subscriptions[new_sub.id] = new_sub

        user = self.user_repo.users.get(user_id)
        if user:
            user.current_plan_code = plan.code.lower()
            user.updated_at = now

        return new_sub

    async def cancel_active_subscription(
        self, user_id: uuid.UUID
    ) -> Subscription | None:
        now = datetime.now(UTC)
        for sub in self.subscriptions.values():
            if sub.user_id == user_id and sub.status == "active":
                sub.status = "cancelled"
                sub.cancelled_at = now
                sub.updated_at = now
                user = self.user_repo.users.get(user_id)
                if user:
                    user.current_plan_code = "free"
                return sub
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_user_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def fake_audit_repo() -> FakeAuditRepository:
    return FakeAuditRepository()


@pytest.fixture
def fake_payment_repo(fake_user_repo: FakeUserRepository) -> FakePaymentRepository:
    return FakePaymentRepository(user_repo=fake_user_repo)


@pytest.fixture
def admin_service(
    fake_user_repo: FakeUserRepository,
    fake_audit_repo: FakeAuditRepository,
    fake_payment_repo: FakePaymentRepository,
) -> AdminUserService:
    return AdminUserService(
        user_repo=fake_user_repo,
        audit_repo=fake_audit_repo,
        payment_repo=fake_payment_repo,
    )


@pytest_asyncio.fixture
async def admin_user(fake_user_repo: FakeUserRepository) -> User:
    """Pre-create an active administrator user."""
    password_hash = hash_password("AdminSecurePass123!")
    return await fake_user_repo.create_user(
        email="admin@edusense.edu.vn",
        password_hash=password_hash,
        full_name="Admin Administrator",
        role_name="admin",
    )


@pytest_asyncio.fixture
async def student_user(fake_user_repo: FakeUserRepository) -> User:
    """Pre-create an active standard student user."""
    password_hash = hash_password("StudentPass123!")
    return await fake_user_repo.create_user(
        email="student.john@university.edu",
        password_hash=password_hash,
        full_name="John Student",
        role_name="student",
        university="Technology University",
        major="Computer Science",
    )


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_audit_repo: FakeAuditRepository,
    fake_payment_repo: FakePaymentRepository,
    admin_service: AdminUserService,
) -> AsyncGenerator[AsyncClient, None]:
    """Test client with dependency overrides."""
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_audit_repo] = lambda: fake_audit_repo
    app.dependency_overrides[get_payment_repo] = lambda: fake_payment_repo
    app.dependency_overrides[get_admin_user_service] = lambda: admin_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """Helper to generate Authorization header with access token."""
    token = create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(hours=1),
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. RBAC Authorization Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_endpoint_unauthenticated_returns_401(client: AsyncClient) -> None:
    """Unauthenticated requests to /api/v1/admin/users MUST receive 401 Unauthorized."""
    resp = await client.get("/api/v1/admin/users")
    assert resp.status_code == 401
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"


@pytest.mark.asyncio
async def test_admin_endpoint_student_role_returns_403(
    client: AsyncClient, student_user: User
) -> None:
    """Student role accessing /api/v1/admin/users MUST receive 403 Forbidden."""
    headers = make_auth_headers(student_user.id)
    resp = await client.get("/api/v1/admin/users", headers=headers)
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_403"
    assert body["error"]["message"] == "Admin privileges required."


@pytest.mark.asyncio
async def test_admin_endpoint_inactive_admin_returns_403(
    client: AsyncClient, admin_user: User
) -> None:
    """Inactive admin accessing /api/v1/admin/users MUST receive 403 Forbidden."""
    admin_user.is_active = False
    headers = make_auth_headers(admin_user.id)
    resp = await client.get("/api/v1/admin/users", headers=headers)
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_403"
    assert "Inactive user account" in body["error"]["message"]


@pytest.mark.asyncio
async def test_admin_endpoint_admin_role_returns_200(
    client: AsyncClient, admin_user: User, student_user: User
) -> None:
    """Admin role accessing /api/v1/admin/users receives 200 OK."""
    headers = make_auth_headers(admin_user.id)
    resp = await client.get("/api/v1/admin/users", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "data" in body
    assert "meta" in body


# ---------------------------------------------------------------------------
# 2. Student Accounts Listing & Filtering Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_students_excludes_admin_accounts(
    client: AsyncClient,
    admin_user: User,
    student_user: User,
) -> None:
    """Verify that admin accounts are NOT listed among students."""
    headers = make_auth_headers(admin_user.id)
    resp = await client.get("/api/v1/admin/users", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["data"]
    emails = [item["email"] for item in items]
    assert admin_user.email not in emails
    assert student_user.email in emails


@pytest.mark.asyncio
async def test_list_students_pagination_meta(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    admin_user: User,
) -> None:
    """Verify pagination metadata calculation."""
    for i in range(25):
        await fake_user_repo.create_user(
            email=f"student_{i}@test.com",
            password_hash="hash",
            full_name=f"Student {i}",
            role_name="student",
        )

    headers = make_auth_headers(admin_user.id)
    resp = await client.get("/api/v1/admin/users?page=1&page_size=10", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 10
    meta = body["meta"]
    assert meta["page"] == 1
    assert meta["page_size"] == 10
    assert meta["total_count"] == 25
    assert meta["total_pages"] == 3


@pytest.mark.asyncio
async def test_list_students_search_by_email_and_name(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    admin_user: User,
) -> None:
    """Verify search filter matches either student email or full name."""
    await fake_user_repo.create_user(
        email="alex.smith@test.com",
        password_hash="hash",
        full_name="Alex Smith",
        role_name="student",
    )
    await fake_user_repo.create_user(
        email="betty.jones@test.com",
        password_hash="hash",
        full_name="Betty Jones",
        role_name="student",
    )

    headers = make_auth_headers(admin_user.id)

    # Search by email fragment
    resp_email = await client.get(
        "/api/v1/admin/users?search=alex.smith", headers=headers
    )
    assert resp_email.status_code == 200
    assert len(resp_email.json()["data"]) == 1
    assert resp_email.json()["data"][0]["email"] == "alex.smith@test.com"

    # Search by name fragment
    resp_name = await client.get("/api/v1/admin/users?search=Jones", headers=headers)
    assert resp_name.status_code == 200
    assert len(resp_name.json()["data"]) == 1
    assert resp_name.json()["data"][0]["full_name"] == "Betty Jones"


@pytest.mark.asyncio
async def test_list_students_filter_is_active(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    admin_user: User,
) -> None:
    """Verify filtering by active status."""
    await fake_user_repo.create_user(
        email="active@test.com",
        password_hash="hash",
        full_name="Active Student",
        role_name="student",
    )
    u2 = await fake_user_repo.create_user(
        email="suspended@test.com",
        password_hash="hash",
        full_name="Suspended Student",
        role_name="student",
    )
    u2.is_active = False

    headers = make_auth_headers(admin_user.id)

    # Filter active
    resp_active = await client.get(
        "/api/v1/admin/users?is_active=true", headers=headers
    )
    assert resp_active.status_code == 200
    for u in resp_active.json()["data"]:
        assert u["is_active"] is True

    # Filter inactive
    resp_inactive = await client.get(
        "/api/v1/admin/users?is_active=false", headers=headers
    )
    assert resp_inactive.status_code == 200
    for u in resp_inactive.json()["data"]:
        assert u["is_active"] is False


@pytest.mark.asyncio
async def test_list_students_filter_plan_code(
    client: AsyncClient,
    fake_user_repo: FakeUserRepository,
    admin_user: User,
) -> None:
    """Verify filtering by current subscription plan code."""
    await fake_user_repo.create_user(
        email="free_user@test.com",
        password_hash="hash",
        full_name="Free User",
        role_name="student",
        current_plan_code="free",
    )
    await fake_user_repo.create_user(
        email="pro_user@test.com",
        password_hash="hash",
        full_name="Pro User",
        role_name="student",
        current_plan_code="pro",
    )

    headers = make_auth_headers(admin_user.id)
    resp = await client.get("/api/v1/admin/users?plan_code=pro", headers=headers)
    assert resp.status_code == 200
    for u in resp.json()["data"]:
        assert u["current_plan_code"] == "pro"


# ---------------------------------------------------------------------------
# 3. Student Detail Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_student_detail_success_200(
    client: AsyncClient,
    admin_user: User,
    student_user: User,
) -> None:
    """Retrieve full details of a student account."""
    headers = make_auth_headers(admin_user.id)
    resp = await client.get(f"/api/v1/admin/users/{student_user.id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["id"] == str(student_user.id)
    assert data["email"] == student_user.email
    assert data["full_name"] == "John Student"
    assert data["profile"]["university"] == "Technology University"
    assert data["subscription"]["plan_code"] == "free"


@pytest.mark.asyncio
async def test_get_student_detail_with_active_pro_subscription(
    client: AsyncClient,
    admin_user: User,
    student_user: User,
    fake_payment_repo: FakePaymentRepository,
) -> None:
    """Verify student detail reflects active Pro subscription."""
    pro_plan = fake_payment_repo.plans["pro"]
    await fake_payment_repo.override_user_subscription(
        user_id=student_user.id,
        plan=pro_plan,
        duration_days=30,
    )

    headers = make_auth_headers(admin_user.id)
    resp = await client.get(f"/api/v1/admin/users/{student_user.id}", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["current_plan_code"] == "pro"
    assert data["subscription"]["plan_code"] == "pro"
    assert data["subscription"]["is_active"] is True
    assert data["subscription"]["days_remaining"] > 0


@pytest.mark.asyncio
async def test_get_student_detail_nonexistent_returns_404(
    client: AsyncClient, admin_user: User
) -> None:
    """Querying nonexistent user ID returns 404."""
    headers = make_auth_headers(admin_user.id)
    random_id = uuid.uuid4()
    resp = await client.get(f"/api/v1/admin/users/{random_id}", headers=headers)
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_404"
    assert f"Student with ID '{random_id}' not found." in body["error"]["message"]


@pytest.mark.asyncio
async def test_get_student_detail_admin_id_returns_404(
    client: AsyncClient, admin_user: User
) -> None:
    """Attempting to view an administrator account via student endpoint returns 404."""
    headers = make_auth_headers(admin_user.id)
    resp = await client.get(f"/api/v1/admin/users/{admin_user.id}", headers=headers)
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_404"
    assert "is not a student" in body["error"]["message"]


# ---------------------------------------------------------------------------
# 4. Student Account Suspension & Activation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suspend_student_success(
    client: AsyncClient,
    admin_user: User,
    student_user: User,
    fake_user_repo: FakeUserRepository,
    fake_audit_repo: FakeAuditRepository,
) -> None:
    """Suspend a student account: verifies is_active=False, sessions revoked, audit event logged."""
    headers = make_auth_headers(admin_user.id)
    payload = {
        "is_active": False,
        "reason": "Suspected academic misconduct and abusive requests.",
    }
    resp = await client.patch(
        f"/api/v1/admin/users/{student_user.id}/status",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["is_active"] is False

    # Verify repository state
    updated_user = await fake_user_repo.get_by_id(student_user.id)
    assert updated_user is not None
    assert updated_user.is_active is False
    assert student_user.id in fake_user_repo.revoked_user_ids

    # Verify audit event
    assert len(fake_audit_repo.events) == 1
    event = fake_audit_repo.events[0]
    assert event.event_type == "account_suspended"
    assert event.severity == "warning"
    assert event.actor_user_id == admin_user.id
    assert event.target_user_id == student_user.id
    assert event.details is not None
    assert event.details["reason"] == payload["reason"]


@pytest.mark.asyncio
async def test_suspend_student_self_suspension_guard_returns_400(
    client: AsyncClient, admin_user: User
) -> None:
    """An administrator cannot suspend their own admin account."""
    headers = make_auth_headers(admin_user.id)
    payload = {"is_active": False, "reason": "Accidental self-suspension"}
    resp = await client.patch(
        f"/api/v1/admin/users/{admin_user.id}/status",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_400"
    assert "cannot suspend their own account" in body["error"]["message"]


@pytest.mark.asyncio
async def test_activate_student_success(
    client: AsyncClient,
    admin_user: User,
    student_user: User,
    fake_audit_repo: FakeAuditRepository,
) -> None:
    """Reactivate a suspended student account."""
    student_user.is_active = False
    headers = make_auth_headers(admin_user.id)
    payload = {
        "is_active": True,
        "reason": "Account appeal approved by department chair.",
    }
    resp = await client.patch(
        f"/api/v1/admin/users/{student_user.id}/status",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["is_active"] is True

    # Verify audit event
    assert len(fake_audit_repo.events) == 1
    event = fake_audit_repo.events[0]
    assert event.event_type == "account_activated"
    assert event.severity == "info"
    assert event.actor_user_id == admin_user.id
    assert event.target_user_id == student_user.id


@pytest.mark.asyncio
async def test_suspend_student_whitespace_reason_returns_422(
    client: AsyncClient,
    admin_user: User,
    student_user: User,
) -> None:
    """Whitespace-only reason must fail validation with 422."""
    headers = make_auth_headers(admin_user.id)
    payload = {"is_active": False, "reason": "   "}
    resp = await client.patch(
        f"/api/v1/admin/users/{student_user.id}/status",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 5. Subscription Plan Override Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_student_plan_to_pro_success(
    client: AsyncClient,
    admin_user: User,
    student_user: User,
    fake_audit_repo: FakeAuditRepository,
) -> None:
    """Manually override student plan from free to pro with audit event."""
    headers = make_auth_headers(admin_user.id)
    payload = {
        "plan_code": "pro",
        "duration_days": 60,
        "reason": "Scholarship grant reward.",
    }
    resp = await client.patch(
        f"/api/v1/admin/users/{student_user.id}/plan",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["current_plan_code"] == "pro"
    assert data["subscription"]["plan_code"] == "pro"
    assert data["subscription"]["is_active"] is True

    # Audit event verified
    assert len(fake_audit_repo.events) == 1
    event = fake_audit_repo.events[0]
    assert event.event_type == "plan_override"
    assert event.severity == "info"
    assert event.actor_user_id == admin_user.id
    assert event.target_user_id == student_user.id
    assert event.details is not None
    assert event.details["plan_code"] == "pro"
    assert event.details["duration_days"] == 60


@pytest.mark.asyncio
async def test_override_student_plan_to_free_success(
    client: AsyncClient,
    admin_user: User,
    student_user: User,
    fake_payment_repo: FakePaymentRepository,
    fake_audit_repo: FakeAuditRepository,
) -> None:
    """Manually downgrade student plan from pro to free."""
    # First assign Pro
    await fake_payment_repo.override_user_subscription(
        student_user.id, fake_payment_repo.plans["pro"], 30
    )

    headers = make_auth_headers(admin_user.id)
    payload = {
        "plan_code": "free",
        "reason": "Requested subscription reset.",
    }
    resp = await client.patch(
        f"/api/v1/admin/users/{student_user.id}/plan",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["current_plan_code"] == "free"
    assert data["subscription"]["plan_code"] == "free"

    # Audit event logged
    assert len(fake_audit_repo.events) == 1
    event = fake_audit_repo.events[0]
    assert event.event_type == "plan_override"
    assert event.details is not None
    assert event.details["plan_code"] == "free"


@pytest.mark.asyncio
async def test_override_student_plan_invalid_plan_returns_400(
    client: AsyncClient,
    admin_user: User,
    student_user: User,
) -> None:
    """Nonexistent plan code returns 400 Bad Request."""
    headers = make_auth_headers(admin_user.id)
    payload = {"plan_code": "vip_diamond_ultra", "reason": "Invalid plan test"}
    resp = await client.patch(
        f"/api/v1/admin/users/{student_user.id}/plan",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_400"
    assert "does not exist" in body["error"]["message"]


@pytest.mark.asyncio
async def test_override_student_plan_nonexistent_student_returns_404(
    client: AsyncClient, admin_user: User
) -> None:
    """Plan override on nonexistent student ID returns 404."""
    headers = make_auth_headers(admin_user.id)
    random_id = uuid.uuid4()
    payload = {"plan_code": "pro", "reason": "Test non-existent"}
    resp = await client.patch(
        f"/api/v1/admin/users/{random_id}/plan",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 6. Service Unit Tests & Edge Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_self_suspension_raises_admin_action_error(
    admin_service: AdminUserService,
    admin_user: User,
) -> None:
    """Service self-suspension guard raises AdminActionError."""
    admin_dto = UserDTO(
        id=admin_user.id,
        email=admin_user.email,
        full_name="Admin",
        role="admin",
        current_plan_code="free",
        is_active=True,
        is_email_verified=True,
        created_at=datetime.now(UTC),
    )
    with pytest.raises(AdminActionError, match="cannot suspend their own account"):
        await admin_service.update_student_status(
            admin_user=admin_dto,
            student_id=admin_user.id,
            is_active=False,
            reason="Self test",
        )


@pytest.mark.asyncio
async def test_service_get_student_detail_non_student_raises_not_found(
    admin_service: AdminUserService,
    admin_user: User,
) -> None:
    """Service raises AdminUserNotFoundError when target user is not a student."""
    with pytest.raises(AdminUserNotFoundError, match="is not a student"):
        await admin_service.get_student_detail(student_id=admin_user.id)


# ---------------------------------------------------------------------------
# 7. Real Repository SQL Construction & Model Mapping Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_repo_log_security_event_model() -> None:
    """Verify AuditRepository.log_security_event flushes AuditSecurityEvent properly."""
    mock_session = MagicMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    repo = AuditRepository(session=mock_session)

    actor_id = uuid.uuid4()
    target_id = uuid.uuid4()

    event = await repo.log_security_event(
        event_type="test_event",
        actor_user_id=actor_id,
        target_user_id=target_id,
        severity="warning",
        ip_address="127.0.0.1",
        user_agent="pytest/test-runner",
        details={"key": "value"},
    )

    mock_session.add.assert_called_once()
    mock_session.flush.assert_awaited_once()
    assert event.event_type == "test_event"
    assert event.actor_user_id == actor_id
    assert event.target_user_id == target_id
    assert event.severity == "warning"
    assert event.ip_address == "127.0.0.1"
    assert event.details == {"key": "value"}


@pytest.mark.asyncio
async def test_user_repo_update_user_status_model() -> None:
    """Verify UserRepository.update_user_status updates user and flushes session."""
    mock_session = MagicMock()
    mock_result = MagicMock()
    user_id = uuid.uuid4()

    mock_user = User(
        id=user_id,
        email="test@edu.vn",
        password_hash="hash",
        is_active=True,
        current_plan_code="free",
    )
    mock_result.scalar_one_or_none.return_value = mock_user
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()

    repo = UserRepository(session=mock_session)
    updated = await repo.update_user_status(user_id=user_id, is_active=False)

    assert updated is not None
    assert updated.is_active is False
    mock_session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_payment_repo_override_user_subscription_model() -> None:
    """Verify PaymentRepository.override_user_subscription executes update and commit."""
    mock_session = MagicMock()
    mock_session.execute = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()

    repo = PaymentRepository(session=mock_session)
    user_id = uuid.uuid4()
    plan = SubscriptionPlan(
        id=uuid.uuid4(),
        code="pro",
        name="Pro",
        duration_days=30,
        is_active=True,
    )

    sub = await repo.override_user_subscription(
        user_id=user_id,
        plan=plan,
        duration_days=60,
    )

    assert sub is not None
    assert sub.user_id == user_id
    assert sub.plan_id == plan.id
    assert sub.status == "active"
    mock_session.commit.assert_awaited_once()
    mock_session.refresh.assert_awaited_once()
