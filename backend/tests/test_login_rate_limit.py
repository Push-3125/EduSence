"""Comprehensive tests for Login Attempt Limiting & Audit Security Logging (NFR04)."""

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Ensure backend root is on sys.path for direct repo-root test execution
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.auth import get_client_ip
from app.api.v1.auth import router as auth_router
from app.core.security import hash_password, hash_token
from app.main import app
from app.repositories.audit_repo import (
    AuditRepository,
    LoginAttempt,
    get_audit_repo,
)
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.auth_service import (
    AuthService,
    InvalidCredentialsError,
    RateLimitExceededError,
    get_auth_service,
)

# Ensure auth router is mounted on app for test runs
if not any(getattr(route, "path", "").startswith("/api/v1/auth") for route in app.routes):
    app.include_router(auth_router)


# ---------------------------------------------------------------------------
# Test Doubles: In-Memory FakeUserRepository and FakeAuditRepository
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for rate-limiting tests."""

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

    async def get_by_email(self, email: str) -> User | None:
        normalized = email.lower().strip()
        for u in self.users.values():
            if u.email.lower().strip() == normalized:
                return u
        return None

    async def create_user(
        self,
        email: str,
        password_hash: str,
        full_name: str,
        role_name: str = "student",
        current_plan_code: str = "free",
        is_email_verified: bool = False,
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
            current_plan_code=current_plan_code,
            is_active=True,
            is_email_verified=is_email_verified,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        profile = UserProfile(
            id=uuid.uuid4(),
            user_id=user_id,
            full_name=full_name.strip(),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        user.profile = profile
        user.roles = [role]
        self.users[user_id] = user
        return user

    async def update_last_login(
        self,
        user_id: uuid.UUID,
        login_at: datetime | None = None,
    ) -> None:
        user = self.users.get(user_id)
        if user:
            user.last_login_at = login_at or datetime.now(UTC)

    async def create_refresh_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return MagicMock()


class FakeAuditRepository(AuditRepository):
    """In-memory AuditRepository double for isolated unit and rate-limiting tests."""

    def __init__(self) -> None:
        self.attempts: list[LoginAttempt] = []

    async def record_login_attempt(
        self,
        identifier_hash: str,
        ip_address: str | None = None,
        succeeded: bool = False,
        attempted_at: datetime | None = None,
    ) -> LoginAttempt:
        attempt = LoginAttempt(
            id=uuid.uuid4(),
            identifier_hash=identifier_hash,
            ip_address=ip_address,
            succeeded=succeeded,
            attempted_at=attempted_at or datetime.now(UTC),
        )
        self.attempts.append(attempt)
        return attempt

    async def count_recent_failures(
        self,
        identifier_hash: str,
        window_minutes: int = 15,
        now: datetime | None = None,
    ) -> int:
        current_time = now or datetime.now(UTC)
        window_start = current_time - timedelta(minutes=window_minutes)

        successes = [
            a for a in self.attempts
            if a.identifier_hash == identifier_hash and a.succeeded
        ]
        latest_success_at = max((a.attempted_at for a in successes), default=None)
        if latest_success_at and latest_success_at.tzinfo is None:
            latest_success_at = latest_success_at.replace(tzinfo=UTC)

        cutoff = window_start
        strict = False
        if latest_success_at and latest_success_at > window_start:
            cutoff = latest_success_at
            strict = True

        failures = [
            a for a in self.attempts
            if a.identifier_hash == identifier_hash
            and not a.succeeded
            and ((a.attempted_at > cutoff) if strict else (a.attempted_at >= cutoff))
        ]
        return len(failures)

    async def get_lockout_remaining_seconds(
        self,
        identifier_hash: str,
        max_failures: int = 5,
        window_minutes: int = 15,
        now: datetime | None = None,
    ) -> int:
        current_time = now or datetime.now(UTC)
        window_start = current_time - timedelta(minutes=window_minutes)

        successes = [
            a for a in self.attempts
            if a.identifier_hash == identifier_hash and a.succeeded
        ]
        latest_success_at = max((a.attempted_at for a in successes), default=None)
        if latest_success_at and latest_success_at.tzinfo is None:
            latest_success_at = latest_success_at.replace(tzinfo=UTC)

        cutoff = window_start
        strict = False
        if latest_success_at and latest_success_at > window_start:
            cutoff = latest_success_at
            strict = True

        failures = [
            a for a in self.attempts
            if a.identifier_hash == identifier_hash
            and not a.succeeded
            and ((a.attempted_at > cutoff) if strict else (a.attempted_at >= cutoff))
        ]

        if len(failures) < max_failures:
            return 0

        # Sort descending by attempted_at
        sorted_failures = sorted(failures, key=lambda a: a.attempted_at, reverse=True)
        threshold_attempt = sorted_failures[max_failures - 1]
        threshold_time = threshold_attempt.attempted_at
        if threshold_time.tzinfo is None:
            threshold_time = threshold_time.replace(tzinfo=UTC)

        lockout_expires_at = threshold_time + timedelta(minutes=window_minutes)
        remaining = int((lockout_expires_at - current_time).total_seconds())
        return max(1, remaining) if remaining > 0 else 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_user_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def fake_audit_repo() -> FakeAuditRepository:
    return FakeAuditRepository()


@pytest_asyncio.fixture
async def active_user(fake_user_repo: FakeUserRepository) -> User:
    return await fake_user_repo.create_user(
        email="student@university.edu.vn",
        password_hash=hash_password("ValidPassword123!"),
        full_name="Active Student",
        is_email_verified=True,
    )


@pytest_asyncio.fixture
async def second_user(fake_user_repo: FakeUserRepository) -> User:
    return await fake_user_repo.create_user(
        email="second.student@university.edu.vn",
        password_hash=hash_password("SecondPassword123!"),
        full_name="Second Student",
        is_email_verified=True,
    )


@pytest_asyncio.fixture
async def client(
    fake_user_repo: FakeUserRepository,
    fake_audit_repo: FakeAuditRepository,
) -> AsyncGenerator[AsyncClient, None]:
    """Test HTTP client with dependency overrides for UserRepository and AuditRepository."""
    service = AuthService(user_repo=fake_user_repo, audit_repo=fake_audit_repo)
    app.dependency_overrides[get_user_repo] = lambda: fake_user_repo
    app.dependency_overrides[get_audit_repo] = lambda: fake_audit_repo
    app.dependency_overrides[get_auth_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Repository Layer Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_repo_record_login_attempt_with_mock_session() -> None:
    """Verify AuditRepository.record_login_attempt adds record to session and flushes."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()

    repo = AuditRepository(session=mock_session)
    attempt = await repo.record_login_attempt(
        identifier_hash="test_identifier_hash_123",
        ip_address="192.168.1.100",
        succeeded=True,
    )

    assert attempt.identifier_hash == "test_identifier_hash_123"
    assert attempt.ip_address == "192.168.1.100"
    assert attempt.succeeded is True
    assert attempt.attempted_at is not None
    mock_session.add.assert_called_once_with(attempt)
    mock_session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_audit_repo_count_recent_failures_sql_execution() -> None:
    """Verify AuditRepository.count_recent_failures executes select query with mock session."""
    mock_session = AsyncMock()
    mock_success_result = MagicMock()
    mock_success_result.scalar_one_or_none.return_value = None

    mock_count_result = MagicMock()
    mock_count_result.scalar_one.return_value = 3

    mock_session.execute.side_effect = [mock_success_result, mock_count_result]

    repo = AuditRepository(session=mock_session)
    failures = await repo.count_recent_failures(
        identifier_hash="hash_abc",
        window_minutes=15,
    )

    assert failures == 3
    assert mock_session.execute.await_count == 2


@pytest.mark.asyncio
async def test_fake_audit_repo_record_and_count_failures(
    fake_audit_repo: FakeAuditRepository,
) -> None:
    """Verify FakeAuditRepository records attempts and counts failed logins."""
    ident = "user_hash_1"
    assert await fake_audit_repo.count_recent_failures(ident) == 0

    await fake_audit_repo.record_login_attempt(ident, succeeded=False)
    await fake_audit_repo.record_login_attempt(ident, succeeded=False)
    await fake_audit_repo.record_login_attempt(ident, succeeded=False)

    assert await fake_audit_repo.count_recent_failures(ident) == 3


@pytest.mark.asyncio
async def test_fake_audit_repo_successes_not_counted_as_failures(
    fake_audit_repo: FakeAuditRepository,
) -> None:
    """Verify successful login attempts are excluded from failure count."""
    ident = "user_hash_2"
    await fake_audit_repo.record_login_attempt(ident, succeeded=True)
    await fake_audit_repo.record_login_attempt(ident, succeeded=True)

    assert await fake_audit_repo.count_recent_failures(ident) == 0


@pytest.mark.asyncio
async def test_fake_audit_repo_window_expiry(
    fake_audit_repo: FakeAuditRepository,
) -> None:
    """Verify failed attempts outside the 15-minute window are ignored."""
    ident = "user_hash_3"
    old_time = datetime.now(UTC) - timedelta(minutes=20)
    recent_time = datetime.now(UTC) - timedelta(minutes=5)

    await fake_audit_repo.record_login_attempt(
        ident, succeeded=False, attempted_at=old_time
    )
    await fake_audit_repo.record_login_attempt(
        ident, succeeded=False, attempted_at=recent_time
    )

    # Only the recent one within 15 minutes should be counted
    assert await fake_audit_repo.count_recent_failures(ident, window_minutes=15) == 1


@pytest.mark.asyncio
async def test_fake_audit_repo_consecutive_reset_on_successful_login(
    fake_audit_repo: FakeAuditRepository,
) -> None:
    """Verify consecutive failed attempts count resets after a successful login."""
    ident = "user_hash_4"
    t1 = datetime.now(UTC) - timedelta(minutes=10)
    t2 = datetime.now(UTC) - timedelta(minutes=8)
    t_success = datetime.now(UTC) - timedelta(minutes=5)
    t3 = datetime.now(UTC) - timedelta(minutes=2)

    await fake_audit_repo.record_login_attempt(ident, succeeded=False, attempted_at=t1)
    await fake_audit_repo.record_login_attempt(ident, succeeded=False, attempted_at=t2)
    await fake_audit_repo.record_login_attempt(ident, succeeded=True, attempted_at=t_success)
    await fake_audit_repo.record_login_attempt(ident, succeeded=False, attempted_at=t3)

    # Failures t1 and t2 preceded t_success; only t3 occurred after success
    assert await fake_audit_repo.count_recent_failures(ident, window_minutes=15) == 1


@pytest.mark.asyncio
async def test_fake_audit_repo_lockout_remaining_seconds_calculation(
    fake_audit_repo: FakeAuditRepository,
) -> None:
    """Verify lockout remaining seconds returns 0 below threshold and > 0 at threshold."""
    ident = "user_hash_5"
    now = datetime.now(UTC)

    # 4 failures: no lockout
    for i in range(4):
        await fake_audit_repo.record_login_attempt(
            ident, succeeded=False, attempted_at=now - timedelta(minutes=4 - i)
        )
    assert await fake_audit_repo.get_lockout_remaining_seconds(ident, max_failures=5) == 0

    # 5th failure: lockout triggered
    await fake_audit_repo.record_login_attempt(
        ident, succeeded=False, attempted_at=now
    )
    remaining = await fake_audit_repo.get_lockout_remaining_seconds(
        ident, max_failures=5, window_minutes=15, now=now
    )
    assert 0 < remaining <= 900

    # Move time forward by 16 minutes: lockout expired
    future = now + timedelta(minutes=16)
    assert await fake_audit_repo.get_lockout_remaining_seconds(
        ident, max_failures=5, window_minutes=15, now=future
    ) == 0


# ---------------------------------------------------------------------------
# 2. Service Layer Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_service_successful_login_records_audit_success(
    fake_user_repo: FakeUserRepository,
    fake_audit_repo: FakeAuditRepository,
    active_user: User,
) -> None:
    """Verify AuthService records succeeded=True audit entry on successful login."""
    service = AuthService(user_repo=fake_user_repo, audit_repo=fake_audit_repo)
    tokens = await service.login_user(
        email=active_user.email,
        password="ValidPassword123!",
        ip_address="192.168.1.50",
    )

    assert tokens.access_token is not None
    assert len(fake_audit_repo.attempts) == 1
    attempt = fake_audit_repo.attempts[0]
    assert attempt.identifier_hash == hash_token(active_user.email.lower().strip())
    assert attempt.ip_address == "192.168.1.50"
    assert attempt.succeeded is True


@pytest.mark.asyncio
async def test_auth_service_failed_password_records_audit_failure(
    fake_user_repo: FakeUserRepository,
    fake_audit_repo: FakeAuditRepository,
    active_user: User,
) -> None:
    """Verify AuthService records succeeded=False audit entry on incorrect password."""
    service = AuthService(user_repo=fake_user_repo, audit_repo=fake_audit_repo)

    with pytest.raises(InvalidCredentialsError):
        await service.login_user(
            email=active_user.email,
            password="WrongPassword999!",
            ip_address="10.0.0.1",
        )

    assert len(fake_audit_repo.attempts) == 1
    attempt = fake_audit_repo.attempts[0]
    assert attempt.identifier_hash == hash_token(active_user.email.lower().strip())
    assert attempt.ip_address == "10.0.0.1"
    assert attempt.succeeded is False


@pytest.mark.asyncio
async def test_auth_service_unknown_email_records_audit_failure(
    fake_user_repo: FakeUserRepository,
    fake_audit_repo: FakeAuditRepository,
) -> None:
    """Verify AuthService records succeeded=False audit entry for non-existent account."""
    service = AuthService(user_repo=fake_user_repo, audit_repo=fake_audit_repo)
    unknown_email = "nonexistent.user@edu.vn"

    with pytest.raises(InvalidCredentialsError):
        await service.login_user(
            email=unknown_email,
            password="SomePassword123!",
            ip_address="172.16.0.5",
        )

    assert len(fake_audit_repo.attempts) == 1
    attempt = fake_audit_repo.attempts[0]
    assert attempt.identifier_hash == hash_token(unknown_email.lower().strip())
    assert attempt.ip_address == "172.16.0.5"
    assert attempt.succeeded is False


@pytest.mark.asyncio
async def test_auth_service_5_failures_triggers_rate_limit_exceeded_error(
    fake_user_repo: FakeUserRepository,
    fake_audit_repo: FakeAuditRepository,
    active_user: User,
) -> None:
    """Verify AuthService raises RateLimitExceededError after 5 consecutive failures."""
    service = AuthService(user_repo=fake_user_repo, audit_repo=fake_audit_repo)

    for _ in range(5):
        with pytest.raises(InvalidCredentialsError):
            await service.login_user(
                email=active_user.email,
                password="WrongPassword!",
            )

    # 6th attempt must be rejected with RateLimitExceededError
    with pytest.raises(RateLimitExceededError) as exc_info:
        await service.login_user(
            email=active_user.email,
            password="ValidPassword123!",
        )

    assert exc_info.value.retry_after_seconds > 0
    assert "Too many failed login attempts" in str(exc_info.value)


@pytest.mark.asyncio
async def test_auth_service_rate_limit_resets_after_success(
    fake_user_repo: FakeUserRepository,
    fake_audit_repo: FakeAuditRepository,
    active_user: User,
) -> None:
    """Verify that a successful login resets the consecutive failure counter."""
    service = AuthService(user_repo=fake_user_repo, audit_repo=fake_audit_repo)

    # 4 failures (not quite locked out)
    for _ in range(4):
        with pytest.raises(InvalidCredentialsError):
            await service.login_user(
                email=active_user.email,
                password="WrongPassword!",
            )

    # User remembers correct password and logs in
    tokens = await service.login_user(
        email=active_user.email,
        password="ValidPassword123!",
    )
    assert tokens.access_token is not None

    # Next attempt should see 0 failures (counter was reset by success)
    ident_hash = hash_token(active_user.email.lower().strip())
    assert await fake_audit_repo.count_recent_failures(ident_hash) == 0


@pytest.mark.asyncio
async def test_auth_service_without_audit_repo_backward_compatible(
    fake_user_repo: FakeUserRepository,
    active_user: User,
) -> None:
    """Verify AuthService operates normally without audit_repo (backward compatibility)."""
    service = AuthService(user_repo=fake_user_repo, audit_repo=None)

    # Can log in normally
    tokens = await service.login_user(
        email=active_user.email,
        password="ValidPassword123!",
    )
    assert tokens.access_token is not None

    # Can fail normally
    with pytest.raises(InvalidCredentialsError):
        await service.login_user(
            email=active_user.email,
            password="WrongPassword!",
        )


# ---------------------------------------------------------------------------
# 3. API Endpoint Integration Tests (/api/v1/auth/login)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_login_success_records_audit(
    client: AsyncClient,
    fake_audit_repo: FakeAuditRepository,
    active_user: User,
) -> None:
    """Verify POST /api/v1/auth/login records succeeded=True on 200 OK."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": active_user.email, "password": "ValidPassword123!"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True

    assert len(fake_audit_repo.attempts) == 1
    assert fake_audit_repo.attempts[0].succeeded is True


@pytest.mark.asyncio
async def test_api_login_wrong_password_records_audit_failure(
    client: AsyncClient,
    fake_audit_repo: FakeAuditRepository,
    active_user: User,
) -> None:
    """Verify POST /api/v1/auth/login records succeeded=False on 401 Unauthorized."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": active_user.email, "password": "WrongPassword999!"},
    )

    assert response.status_code == 401
    assert len(fake_audit_repo.attempts) == 1
    assert fake_audit_repo.attempts[0].succeeded is False


@pytest.mark.asyncio
async def test_api_login_5_consecutive_failures_triggers_429(
    client: AsyncClient,
    active_user: User,
) -> None:
    """Verify 5 consecutive failed logins trigger HTTP 429 Too Many Requests with Retry-After."""
    # Execute 5 failed login attempts
    for i in range(5):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": active_user.email, "password": f"BadPassword{i}!"},
        )
        assert resp.status_code == 401, f"Attempt {i+1} should fail with 401"

    # 6th attempt must trigger HTTP 429 Too Many Requests
    resp_429 = await client.post(
        "/api/v1/auth/login",
        json={"email": active_user.email, "password": "ValidPassword123!"},
    )

    assert resp_429.status_code == 429
    assert "Retry-After" in resp_429.headers
    retry_after_str = resp_429.headers["Retry-After"]
    assert retry_after_str.isdigit()
    assert int(retry_after_str) > 0

    body = resp_429.json()
    assert body["success"] is False
    assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert "Too many failed login attempts" in body["error"]["message"]
    assert body["error"]["retry_after"] == int(retry_after_str)


@pytest.mark.asyncio
async def test_api_login_different_accounts_throttled_independently(
    client: AsyncClient,
    active_user: User,
    second_user: User,
) -> None:
    """Verify account A lockout does not throttle account B."""
    # Lock out active_user (5 failures)
    for _ in range(5):
        await client.post(
            "/api/v1/auth/login",
            json={"email": active_user.email, "password": "WrongPassword!"},
        )

    # active_user is locked out (429)
    resp_locked = await client.post(
        "/api/v1/auth/login",
        json={"email": active_user.email, "password": "ValidPassword123!"},
    )
    assert resp_locked.status_code == 429

    # second_user is NOT locked out (200 OK)
    resp_second = await client.post(
        "/api/v1/auth/login",
        json={"email": second_user.email, "password": "SecondPassword123!"},
    )
    assert resp_second.status_code == 200
    assert resp_second.json()["success"] is True


@pytest.mark.asyncio
async def test_api_login_client_ip_extracted_from_x_forwarded_for(
    client: AsyncClient,
    fake_audit_repo: FakeAuditRepository,
    active_user: User,
) -> None:
    """Verify client IP address is correctly parsed from X-Forwarded-For header."""
    headers = {"X-Forwarded-For": "203.0.113.195, 70.41.3.18, 150.172.238.178"}
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": active_user.email, "password": "ValidPassword123!"},
        headers=headers,
    )

    assert response.status_code == 200
    assert len(fake_audit_repo.attempts) == 1
    assert fake_audit_repo.attempts[0].ip_address == "203.0.113.195"


@pytest.mark.asyncio
async def test_api_login_lockout_recovery_after_window_expiration(
    client: AsyncClient,
    fake_audit_repo: FakeAuditRepository,
    active_user: User,
) -> None:
    """Verify login is accepted after the 15-minute lockout window expires."""
    ident_hash = hash_token(active_user.email.lower().strip())
    fifteen_mins_ago = datetime.now(UTC) - timedelta(minutes=16)

    # Simulate 5 failures from 16 minutes ago
    for _ in range(5):
        await fake_audit_repo.record_login_attempt(
            identifier_hash=ident_hash,
            succeeded=False,
            attempted_at=fifteen_mins_ago,
        )

    # Attempts are now expired; login should succeed (200 OK)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": active_user.email, "password": "ValidPassword123!"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True


@pytest.mark.asyncio
async def test_get_client_ip_direct_and_forwarded() -> None:
    """Unit test get_client_ip helper for multiple header structures."""
    mock_req_forwarded = MagicMock()
    mock_req_forwarded.headers = {"x-forwarded-for": "198.51.100.1, 10.0.0.1"}
    assert get_client_ip(mock_req_forwarded) == "198.51.100.1"

    mock_req_direct = MagicMock()
    mock_req_direct.headers = {}
    mock_req_direct.client.host = "127.0.0.1"
    assert get_client_ip(mock_req_direct) == "127.0.0.1"

    mock_req_none = MagicMock()
    mock_req_none.headers = {}
    mock_req_none.client = None
    assert get_client_ip(mock_req_none) is None
