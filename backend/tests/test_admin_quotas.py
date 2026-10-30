"""Comprehensive test suite for Admin AI Usage Analytics and Quota Policy Configuration (US 2.23 / TASK-M6-05).

Adheres strictly to:
- Invariant 2: Thin router -> Service -> Repository layering.
- Invariant 6: Strict RBAC on admin endpoints (401 unauthenticated, 403 student, 200 admin).
- Check constraints: non-negative limits, warning threshold between 1 and 100.
- Audit event logging: quota_policy_updated events with details.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.admin.quotas import alias_router as admin_quotas_alias_router
from app.api.v1.admin.quotas import router as admin_quotas_router
from app.core.security import create_access_token, hash_password
from app.main import app
from app.repositories.ai_usage_repo import (
    AIUsageLog,
    AIUsageRepository,
    FeatureUsageBreakdown,
    ModelUsageBreakdown,
    StatusUsageBreakdown,
    SystemAnalyticsResult,
    TopUserUsageItem,
    get_ai_usage_repo,
)
from app.repositories.audit_repo import (
    AuditRepository,
    AuditSecurityEvent,
    get_audit_repo,
)
from app.repositories.quota_repo import (
    QuotaPolicy,
    QuotaPolicyNotFoundError,
    QuotaPolicyRepository,
    get_quota_repo,
)
from app.repositories.user_repo import (
    Role,
    SubscriptionPlan,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.admin_service import (
    AdminAIService,
    get_admin_ai_service,
)
from app.services.quota_service import AIQuotaService, get_quota_service

# Ensure routers are mounted for standalone test runs
if not any(
    getattr(route, "path", "").startswith("/api/v1/admin/ai") for route in app.routes
):
    app.include_router(admin_quotas_router)
if not any(
    getattr(route, "path", "").startswith("/api/v1/admin/quotas")
    for route in app.routes
):
    app.include_router(admin_quotas_alias_router)


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
    """In-memory UserRepository double for authentication and admin checks."""

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
            is_email_verified=True,
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
        profile.user = user
        user.profile = profile

        self.users[user_id] = user
        return user


class FakeQuotaPolicyRepository(QuotaPolicyRepository):
    """In-memory QuotaPolicyRepository double."""

    def __init__(self, user_repo: FakeUserRepository) -> None:
        self.user_repo = user_repo
        self.policies: dict[str, QuotaPolicy] = {}
        now = datetime.now(UTC)

        free_plan = SubscriptionPlan(
            id=uuid.uuid4(),
            code="free",
            name="Free Tier",
            price_vnd=0,
            duration_days=None,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        pro_plan = SubscriptionPlan(
            id=uuid.uuid4(),
            code="pro",
            name="Pro Subscription",
            price_vnd=50000,
            duration_days=30,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        self.plans = {"free": free_plan, "pro": pro_plan}

        self.policies["free"] = QuotaPolicy(
            id=uuid.uuid4(),
            plan_id=free_plan.id,
            request_limit_per_day=10,
            token_limit_per_day=None,
            warning_threshold_percent=80,
            updated_by=None,
            created_at=now,
            updated_at=now,
        )
        self.policies["free"].plan = free_plan
        self.policies["free"].updater = None

        self.policies["pro"] = QuotaPolicy(
            id=uuid.uuid4(),
            plan_id=pro_plan.id,
            request_limit_per_day=150,
            token_limit_per_day=None,
            warning_threshold_percent=80,
            updated_by=None,
            created_at=now,
            updated_at=now,
        )
        self.policies["pro"].plan = pro_plan
        self.policies["pro"].updater = None

    async def get_all_policies(self) -> list[QuotaPolicy]:
        return list(self.policies.values())

    async def get_by_plan_code(self, plan_code: str) -> QuotaPolicy | None:
        return self.policies.get(plan_code.strip().lower())

    async def update_policy(
        self,
        plan_code: str,
        request_limit_per_day: int,
        token_limit_per_day: int | None,
        warning_threshold_percent: int,
        updated_by: uuid.UUID,
    ) -> QuotaPolicy:
        if request_limit_per_day < 0:
            raise ValueError("request_limit_per_day must be non-negative (>= 0)")
        if token_limit_per_day is not None and token_limit_per_day < 0:
            raise ValueError("token_limit_per_day must be non-negative (>= 0) or None")
        if not (1 <= warning_threshold_percent <= 100):
            raise ValueError("warning_threshold_percent must be between 1 and 100")

        norm_code = plan_code.strip().lower()
        policy = self.policies.get(norm_code)
        plan = self.plans.get(norm_code)
        if not plan and not policy:
            raise QuotaPolicyNotFoundError(f"Subscription plan '{plan_code}' not found.")

        now = datetime.now(UTC)
        updater_user = self.user_repo.users.get(updated_by)

        if not policy:
            assert plan is not None
            policy = QuotaPolicy(
                id=uuid.uuid4(),
                plan_id=plan.id,
                request_limit_per_day=request_limit_per_day,
                token_limit_per_day=token_limit_per_day,
                warning_threshold_percent=warning_threshold_percent,
                updated_by=updated_by,
                created_at=now,
                updated_at=now,
            )
            policy.plan = plan
            policy.updater = updater_user
            self.policies[norm_code] = policy
        else:
            policy.request_limit_per_day = request_limit_per_day
            policy.token_limit_per_day = token_limit_per_day
            policy.warning_threshold_percent = warning_threshold_percent
            policy.updated_by = updated_by
            policy.updated_at = now
            policy.updater = updater_user

        return policy


class FakeAIUsageRepository(AIUsageRepository):
    """In-memory AIUsageRepository double with telemetry storage and analytics."""

    def __init__(self, user_repo: FakeUserRepository) -> None:
        self.user_repo = user_repo
        self.logs: list[AIUsageLog] = []

    async def create_log(
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
        estimated_cost: Decimal | float = 0.0,
        provider: str = "google",
        created_at: datetime | None = None,
    ) -> AIUsageLog:
        computed_total = prompt_tokens + completion_tokens
        resolved_total = total_tokens if total_tokens is not None else computed_total
        log_entry = AIUsageLog(
            id=uuid.uuid4(),
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            feature=feature.strip().lower(),
            provider=provider.strip().lower(),
            model_name=model_name.strip(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=resolved_total,
            estimated_cost=Decimal(str(estimated_cost)),
            latency_ms=latency_ms,
            request_status=request_status,
            error_message=error_message,
            created_at=created_at or datetime.now(UTC),
        )
        self.logs.append(log_entry)
        return log_entry

    async def get_system_analytics(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> SystemAnalyticsResult:
        filtered = [
            l for l in self.logs if start_time <= l.created_at <= end_time
        ]
        total_requests = len(filtered)
        prompt_tokens = sum(l.prompt_tokens for l in filtered)
        completion_tokens = sum(l.completion_tokens for l in filtered)
        total_tokens = sum(l.total_tokens for l in filtered)
        estimated_cost = float(sum(l.estimated_cost for l in filtered))
        latencies = [l.latency_ms for l in filtered if l.latency_ms is not None]
        avg_latency = float(sum(latencies) / len(latencies)) if latencies else 0.0

        # Group by feature
        by_feature_map: dict[str, list[AIUsageLog]] = {}
        for l in filtered:
            by_feature_map.setdefault(l.feature, []).append(l)

        by_feature = [
            FeatureUsageBreakdown(
                feature=feat,
                total_requests=len(flist),
                prompt_tokens=sum(x.prompt_tokens for x in flist),
                completion_tokens=sum(x.completion_tokens for x in flist),
                total_tokens=sum(x.total_tokens for x in flist),
                estimated_cost=float(sum(x.estimated_cost for x in flist)),
                average_latency_ms=round(
                    float(sum(x.latency_ms or 0 for x in flist) / len(flist)), 2
                )
                if flist
                else 0.0,
            )
            for feat, flist in by_feature_map.items()
        ]

        # Group by model
        by_model_map: dict[str, list[AIUsageLog]] = {}
        for l in filtered:
            by_model_map.setdefault(l.model_name, []).append(l)

        by_model = [
            ModelUsageBreakdown(
                model_name=mod,
                total_requests=len(mlist),
                prompt_tokens=sum(x.prompt_tokens for x in mlist),
                completion_tokens=sum(x.completion_tokens for x in mlist),
                total_tokens=sum(x.total_tokens for x in mlist),
                estimated_cost=float(sum(x.estimated_cost for x in mlist)),
            )
            for mod, mlist in by_model_map.items()
        ]

        # Group by status
        by_status_map: dict[str, int] = {}
        for l in filtered:
            by_status_map[l.request_status] = by_status_map.get(l.request_status, 0) + 1

        by_status = [
            StatusUsageBreakdown(
                request_status=st,
                total_requests=cnt,
            )
            for st, cnt in by_status_map.items()
        ]

        return SystemAnalyticsResult(
            total_requests=total_requests,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost=estimated_cost,
            average_latency_ms=round(avg_latency, 2),
            by_feature=by_feature,
            by_model=by_model,
            by_status=by_status,
        )

    async def get_top_users(
        self,
        limit: int = 10,
        start_time: datetime | None = None,
    ) -> list[TopUserUsageItem]:
        filtered = [
            l for l in self.logs if start_time is None or l.created_at >= start_time
        ]
        by_user: dict[uuid.UUID, list[AIUsageLog]] = {}
        for l in filtered:
            by_user.setdefault(l.user_id, []).append(l)

        items: list[TopUserUsageItem] = []
        for uid, ulogs in by_user.items():
            user = self.user_repo.users.get(uid)
            email = user.email if user else f"user_{uid}@example.com"
            full_name = (
                user.profile.full_name
                if (user and user.profile and user.profile.full_name)
                else email
            )
            items.append(
                TopUserUsageItem(
                    user_id=uid,
                    email=email,
                    full_name=full_name,
                    total_requests=len(ulogs),
                    prompt_tokens=sum(x.prompt_tokens for x in ulogs),
                    completion_tokens=sum(x.completion_tokens for x in ulogs),
                    total_tokens=sum(x.total_tokens for x in ulogs),
                    estimated_cost=float(sum(x.estimated_cost for x in ulogs)),
                )
            )

        items.sort(key=lambda x: x.total_tokens, reverse=True)
        return items[:limit]

    async def get_telemetry_logs(
        self,
        limit: int = 50,
        skip: int = 0,
        feature: str | None = None,
        request_status: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> tuple[list[AIUsageLog], int]:
        filtered = list(self.logs)
        if feature:
            norm_feat = feature.strip().lower()
            filtered = [l for l in filtered if l.feature == norm_feat]
        if request_status:
            norm_st = request_status.strip().lower()
            filtered = [l for l in filtered if l.request_status == norm_st]
        if user_id is not None:
            filtered = [l for l in filtered if l.user_id == user_id]

        filtered.sort(key=lambda l: l.created_at, reverse=True)
        total = len(filtered)
        paginated = filtered[skip : skip + limit]
        return paginated, total


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
def fake_quota_repo(fake_user_repo: FakeUserRepository) -> FakeQuotaPolicyRepository:
    return FakeQuotaPolicyRepository(user_repo=fake_user_repo)


@pytest.fixture
def fake_ai_usage_repo(fake_user_repo: FakeUserRepository) -> FakeAIUsageRepository:
    return FakeAIUsageRepository(user_repo=fake_user_repo)


@pytest.fixture
def mock_quota_service() -> AIQuotaService:
    return AIQuotaService(redis_client=None)


@pytest.fixture
def admin_ai_service(
    fake_quota_repo: FakeQuotaPolicyRepository,
    fake_ai_usage_repo: FakeAIUsageRepository,
    fake_audit_repo: FakeAuditRepository,
    mock_quota_service: AIQuotaService,
) -> AdminAIService:
    return AdminAIService(
        quota_repo=fake_quota_repo,
        ai_usage_repo=fake_ai_usage_repo,
        audit_repo=fake_audit_repo,
        quota_service=mock_quota_service,
    )


@pytest_asyncio.fixture
async def admin_user(fake_user_repo: FakeUserRepository) -> User:
    password_hash = hash_password("AdminSecurePass123!")
    return await fake_user_repo.create_user(
        email="admin@edusense.edu.vn",
        password_hash=password_hash,
        full_name="Admin Administrator",
        role_name="admin",
    )


@pytest_asyncio.fixture
async def student_user(fake_user_repo: FakeUserRepository) -> User:
    password_hash = hash_password("StudentPass123!")
    return await fake_user_repo.create_user(
        email="student.john@university.edu",
        password_hash=password_hash,
        full_name="John Student",
        role_name="student",
    )


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_audit_repo: FakeAuditRepository,
    fake_quota_repo: FakeQuotaPolicyRepository,
    fake_ai_usage_repo: FakeAIUsageRepository,
    mock_quota_service: AIQuotaService,
    admin_ai_service: AdminAIService,
) -> AsyncGenerator[AsyncClient, None]:
    """Test client with mock dependency overrides."""
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_audit_repo] = lambda: fake_audit_repo
    app.dependency_overrides[get_quota_repo] = lambda: fake_quota_repo
    app.dependency_overrides[get_ai_usage_repo] = lambda: fake_ai_usage_repo
    app.dependency_overrides[get_quota_service] = lambda: mock_quota_service
    app.dependency_overrides[get_admin_ai_service] = lambda: admin_ai_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(hours=1),
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. RBAC Security Tests (Invariant 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_quotas_unauthenticated_returns_401(client: AsyncClient) -> None:
    """Unauthenticated requests to /api/v1/admin/ai/quotas MUST receive 401."""
    resp = await client.get("/api/v1/admin/ai/quotas")
    assert resp.status_code == 401
    assert resp.json()["success"] is False


@pytest.mark.asyncio
async def test_list_quotas_student_forbidden_returns_403(
    client: AsyncClient, student_user: User
) -> None:
    """Student account MUST receive 403 Forbidden."""
    headers = make_auth_headers(student_user.id)
    resp = await client.get("/api/v1/admin/ai/quotas", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["success"] is False
    assert resp.json()["error"]["code"] == "HTTP_403"


@pytest.mark.asyncio
async def test_update_quota_student_forbidden_returns_403(
    client: AsyncClient, student_user: User
) -> None:
    """Student modifying quota policy MUST receive 403 Forbidden."""
    headers = make_auth_headers(student_user.id)
    resp = await client.patch(
        "/api/v1/admin/ai/quotas/free",
        json={"request_limit_per_day": 25, "warning_threshold_percent": 75},
        headers=headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_analytics_student_forbidden_returns_403(
    client: AsyncClient, student_user: User
) -> None:
    """Student accessing analytics MUST receive 403 Forbidden."""
    headers = make_auth_headers(student_user.id)
    resp = await client.get("/api/v1/admin/ai/analytics", headers=headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_top_users_student_forbidden_returns_403(
    client: AsyncClient, student_user: User
) -> None:
    """Student accessing top users MUST receive 403 Forbidden."""
    headers = make_auth_headers(student_user.id)
    resp = await client.get("/api/v1/admin/ai/top-users", headers=headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_telemetry_logs_student_forbidden_returns_403(
    client: AsyncClient, student_user: User
) -> None:
    """Student accessing raw telemetry logs MUST receive 403 Forbidden."""
    headers = make_auth_headers(student_user.id)
    resp = await client.get("/api/v1/admin/ai/logs", headers=headers)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 2. Quota Policy Listing & Updating (US 2.23)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_quotas_admin_success(
    client: AsyncClient, admin_user: User
) -> None:
    """Admin successfully lists all quota policies with plan details."""
    headers = make_auth_headers(admin_user.id)
    resp = await client.get("/api/v1/admin/ai/quotas", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["data"]) >= 2
    plan_codes = [p["plan_code"] for p in body["data"]]
    assert "free" in plan_codes
    assert "pro" in plan_codes


@pytest.mark.asyncio
async def test_update_quota_policy_success_and_audit(
    client: AsyncClient,
    admin_user: User,
    fake_audit_repo: FakeAuditRepository,
    mock_quota_service: AIQuotaService,
) -> None:
    """Admin updates Pro plan daily limit; verifies limits and audit security logging."""
    headers = make_auth_headers(admin_user.id)
    payload = {
        "request_limit_per_day": 200,
        "token_limit_per_day": 500000,
        "warning_threshold_percent": 85,
    }
    resp = await client.patch("/api/v1/admin/ai/quotas/pro", json=payload, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["plan_code"] == "pro"
    assert data["request_limit_per_day"] == 200
    assert data["token_limit_per_day"] == 500000
    assert data["warning_threshold_percent"] == 85
    assert data["updated_by"] == str(admin_user.id)
    assert data["updater_email"] == admin_user.email

    # Verify audit security event
    assert len(fake_audit_repo.events) == 1
    event = fake_audit_repo.events[0]
    assert event.event_type == "quota_policy_updated"
    assert event.actor_user_id == admin_user.id
    assert event.severity == "info"
    assert event.details is not None
    assert event.details["plan_code"] == "pro"
    assert event.details["request_limit_per_day"] == 200

    # Verify cache / in-memory quota service updated
    assert mock_quota_service.get_limit_for_plan("pro") == 200
    assert mock_quota_service.warning_threshold_percent == 85.0


@pytest.mark.asyncio
async def test_update_quota_policy_alias_path(
    client: AsyncClient, admin_user: User
) -> None:
    """Verify alias route /api/v1/admin/quotas/{plan_code} works seamlessly."""
    headers = make_auth_headers(admin_user.id)
    payload = {
        "request_limit_per_day": 15,
        "warning_threshold_percent": 90,
    }
    resp = await client.patch("/api/v1/admin/quotas/free", json=payload, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["request_limit_per_day"] == 15
    assert body["data"]["warning_threshold_percent"] == 90


@pytest.mark.asyncio
async def test_update_quota_policy_validation_negative_requests(
    client: AsyncClient, admin_user: User
) -> None:
    """Request limit < 0 triggers Pydantic 422 Unprocessable Entity."""
    headers = make_auth_headers(admin_user.id)
    payload = {"request_limit_per_day": -5, "warning_threshold_percent": 80}
    resp = await client.patch("/api/v1/admin/ai/quotas/pro", json=payload, headers=headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_quota_policy_validation_invalid_warning_percentage(
    client: AsyncClient, admin_user: User
) -> None:
    """Warning threshold outside 1..100 triggers 422."""
    headers = make_auth_headers(admin_user.id)
    payload = {"request_limit_per_day": 100, "warning_threshold_percent": 150}
    resp = await client.patch("/api/v1/admin/ai/quotas/pro", json=payload, headers=headers)
    assert resp.status_code == 422

    payload_zero = {"request_limit_per_day": 100, "warning_threshold_percent": 0}
    resp2 = await client.patch("/api/v1/admin/ai/quotas/pro", json=payload_zero, headers=headers)
    assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_update_quota_policy_nonexistent_plan_returns_404(
    client: AsyncClient, admin_user: User
) -> None:
    """Non-existent subscription plan returns 404 Not Found."""
    headers = make_auth_headers(admin_user.id)
    payload = {"request_limit_per_day": 100, "warning_threshold_percent": 80}
    resp = await client.patch("/api/v1/admin/ai/quotas/enterprise", json=payload, headers=headers)
    assert resp.status_code == 404
    body = resp.json()
    assert "not found" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# 3. AI Usage Analytics Endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ai_analytics_aggregated_totals_and_breakdowns(
    client: AsyncClient,
    admin_user: User,
    student_user: User,
    fake_ai_usage_repo: FakeAIUsageRepository,
) -> None:
    """Admin retrieves AI analytics including totals and feature/model/status breakdowns."""
    headers = make_auth_headers(admin_user.id)

    # Seed usage logs
    await fake_ai_usage_repo.create_log(
        user_id=student_user.id,
        feature="chat",
        model_name="gemini-2.5-flash",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        latency_ms=250,
        request_status="success",
        estimated_cost=Decimal("0.000300"),
    )
    await fake_ai_usage_repo.create_log(
        user_id=student_user.id,
        feature="summary",
        model_name="gemini-2.5-flash",
        prompt_tokens=400,
        completion_tokens=100,
        total_tokens=500,
        latency_ms=600,
        request_status="success",
        estimated_cost=Decimal("0.001000"),
    )
    await fake_ai_usage_repo.create_log(
        user_id=student_user.id,
        feature="questions",
        model_name="gemini-2.5-pro",
        prompt_tokens=200,
        completion_tokens=50,
        total_tokens=250,
        latency_ms=400,
        request_status="failed",
        estimated_cost=Decimal("0.000500"),
    )

    resp = await client.get("/api/v1/admin/ai/analytics?days=7", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]

    # Verify totals
    assert data["total_requests"] == 3
    assert data["prompt_tokens"] == 700
    assert data["completion_tokens"] == 200
    assert data["total_tokens"] == 900
    assert data["estimated_cost"] == pytest.approx(0.0018, rel=1e-3)
    assert data["average_latency_ms"] > 0

    # Verify feature breakdown
    features = {f["feature"]: f for f in data["by_feature"]}
    assert "chat" in features
    assert "summary" in features
    assert "questions" in features
    assert features["chat"]["total_requests"] == 1
    assert features["summary"]["total_tokens"] == 500

    # Verify model breakdown
    models = {m["model_name"]: m for m in data["by_model"]}
    assert "gemini-2.5-flash" in models
    assert "gemini-2.5-pro" in models
    assert models["gemini-2.5-flash"]["total_requests"] == 2

    # Verify status breakdown
    statuses = {s["request_status"]: s["total_requests"] for s in data["by_status"]}
    assert statuses.get("success") == 2
    assert statuses.get("failed") == 1


@pytest.mark.asyncio
async def test_ai_analytics_invalid_days_returns_422(
    client: AsyncClient, admin_user: User
) -> None:
    """days < 1 or days > 365 returns 422 Unprocessable Entity."""
    headers = make_auth_headers(admin_user.id)
    resp = await client.get("/api/v1/admin/ai/analytics?days=0", headers=headers)
    assert resp.status_code == 422

    resp2 = await client.get("/api/v1/admin/ai/analytics?days=400", headers=headers)
    assert resp2.status_code == 422


# ---------------------------------------------------------------------------
# 4. Top AI Consumers Endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_ai_users_ranked(
    client: AsyncClient,
    admin_user: User,
    student_user: User,
    fake_user_repo: FakeUserRepository,
    fake_ai_usage_repo: FakeAIUsageRepository,
) -> None:
    """Admin retrieves ranked list of top AI token consumers."""
    headers = make_auth_headers(admin_user.id)

    # Create second student
    student_2 = await fake_user_repo.create_user(
        email="power.student@edusense.edu",
        password_hash=hash_password("Pass123!"),
        full_name="Power Student",
        role_name="student",
    )

    # Student 1: 500 tokens
    await fake_ai_usage_repo.create_log(
        user_id=student_user.id,
        feature="chat",
        model_name="gemini-2.5-flash",
        prompt_tokens=300,
        completion_tokens=200,
        total_tokens=500,
    )
    # Student 2: 2000 tokens
    await fake_ai_usage_repo.create_log(
        user_id=student_2.id,
        feature="summary",
        model_name="gemini-2.5-flash",
        prompt_tokens=1500,
        completion_tokens=500,
        total_tokens=2000,
    )

    resp = await client.get("/api/v1/admin/ai/top-users?limit=5&days=30", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]

    assert len(data) == 2
    # First must be student_2 with 2000 tokens
    assert data[0]["user_id"] == str(student_2.id)
    assert data[0]["email"] == student_2.email
    assert data[0]["full_name"] == "Power Student"
    assert data[0]["total_tokens"] == 2000

    # Second is student_user with 500 tokens
    assert data[1]["user_id"] == str(student_user.id)
    assert data[1]["total_tokens"] == 500


# ---------------------------------------------------------------------------
# 5. AI Telemetry Logs Endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_logs_pagination_and_filters(
    client: AsyncClient,
    admin_user: User,
    student_user: User,
    fake_ai_usage_repo: FakeAIUsageRepository,
) -> None:
    """Admin fetches paginated logs and filters by feature and request status."""
    headers = make_auth_headers(admin_user.id)

    # Seed 3 logs
    await fake_ai_usage_repo.create_log(
        user_id=student_user.id,
        feature="chat",
        model_name="gemini-2.5-flash",
        prompt_tokens=50,
        completion_tokens=50,
        total_tokens=100,
        request_status="success",
    )
    await fake_ai_usage_repo.create_log(
        user_id=student_user.id,
        feature="chat",
        model_name="gemini-2.5-flash",
        prompt_tokens=40,
        completion_tokens=60,
        total_tokens=100,
        request_status="failed",
    )
    await fake_ai_usage_repo.create_log(
        user_id=student_user.id,
        feature="questions",
        model_name="gemini-2.5-pro",
        prompt_tokens=120,
        completion_tokens=80,
        total_tokens=200,
        request_status="success",
    )

    # 1. Fetch all with pagination
    resp = await client.get(
        "/api/v1/admin/ai/logs?page=1&page_size=2", headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["data"]) == 2
    assert body["meta"]["total_count"] == 3
    assert body["meta"]["total_pages"] == 2

    # 2. Filter by feature='chat'
    resp_feat = await client.get(
        "/api/v1/admin/ai/logs?feature=chat", headers=headers
    )
    assert resp_feat.status_code == 200
    body_feat = resp_feat.json()
    assert body_feat["meta"]["total_count"] == 2
    for item in body_feat["data"]:
        assert item["feature"] == "chat"

    # 3. Filter by status='failed'
    resp_status = await client.get(
        "/api/v1/admin/ai/logs?status=failed", headers=headers
    )
    assert resp_status.status_code == 200
    body_status = resp_status.json()
    assert body_status["meta"]["total_count"] == 1
    assert body_status["data"][0]["request_status"] == "failed"

    # 4. Filter by student user_id
    resp_user = await client.get(
        f"/api/v1/admin/ai/logs?user_id={student_user.id}", headers=headers
    )
    assert resp_user.status_code == 200
    assert resp_user.json()["meta"]["total_count"] == 3
