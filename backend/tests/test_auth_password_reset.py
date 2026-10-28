"""Comprehensive tests for single-use verification token password reset flow (US 2.3)."""

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Ensure backend root is on sys.path for direct repo-root test execution
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.auth import router as auth_router
from app.core.config import settings
from app.core.security import (
    InvalidTokenError,
    hash_password,
    hash_token,
    verify_password,
)
from app.main import app
from app.repositories.user_repo import (
    RefreshSession,
    Role,
    User,
    UserProfile,
    UserRepository,
    VerificationToken,
    get_user_repo,
)
from app.services.auth_service import (
    AuthService,
    WeakPasswordError,
    get_auth_service,
)

# Ensure auth router is mounted on app for test runs
if not any(getattr(route, "path", "").startswith("/api/v1/auth") for route in app.routes):
    app.include_router(auth_router)


# ---------------------------------------------------------------------------
# In-Memory Test Double: FakeUserRepository with VerificationToken Support
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository test double for isolated unit and endpoint testing."""

    def __init__(self) -> None:
        self.users: dict[uuid.UUID, User] = {}
        self.refresh_sessions: dict[uuid.UUID, RefreshSession] = {}
        self.verification_tokens: dict[uuid.UUID, VerificationToken] = {}
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
                description="System administrator role.",
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
    ) -> User:
        role = self.roles.get(role_name)
        if not role:
            role = Role(
                id=uuid.uuid4(),
                name=role_name,
                description=f"Role {role_name}",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            self.roles[role_name] = role

        user = User(
            id=uuid.uuid4(),
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
            user_id=user.id,
            full_name=full_name.strip(),
            avatar_url=avatar_url,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        user.profile = profile
        user.roles = [role]
        self.users[user.id] = user
        return user

    async def get_profile_by_user_id(
        self, user_id: uuid.UUID
    ) -> UserProfile | None:
        user = self.users.get(user_id)
        return user.profile if user else None

    async def update_profile(
        self, user_id: uuid.UUID, **fields: Any
    ) -> UserProfile | None:
        user = self.users.get(user_id)
        if not user or not user.profile:
            return None
        allowed_fields = {
            "full_name",
            "display_name",
            "avatar_url",
            "phone_number",
            "university",
            "major",
            "academic_year",
        }
        for key, value in fields.items():
            if key in allowed_fields and hasattr(user.profile, key):
                setattr(user.profile, key, value)
        user.profile.updated_at = datetime.now(UTC)
        return user.profile

    async def update_user_password(
        self, user_id: uuid.UUID, password_hash: str
    ) -> bool:
        user = self.users.get(user_id)
        if not user:
            return False
        user.password_hash = password_hash
        user.updated_at = datetime.now(UTC)
        return True

    async def update_last_login(
        self, user_id: uuid.UUID, login_at: datetime | None = None
    ) -> None:
        user = self.users.get(user_id)
        if user:
            user.last_login_at = login_at or datetime.now(UTC)

    async def create_refresh_session(
        self,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshSession:
        session = RefreshSession(
            id=uuid.uuid4(),
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
            created_at=datetime.now(UTC),
        )
        self.refresh_sessions[session.id] = session
        return session

    async def get_refresh_session_by_hash(
        self, token_hash: str
    ) -> RefreshSession | None:
        for s in self.refresh_sessions.values():
            if s.token_hash == token_hash:
                return s
        return None

    async def revoke_refresh_session(
        self, token_hash: str, revoked_at: datetime | None = None
    ) -> bool:
        for s in self.refresh_sessions.values():
            if s.token_hash == token_hash and s.revoked_at is None:
                s.revoked_at = revoked_at or datetime.now(UTC)
                return True
        return False

    async def revoke_all_user_sessions(
        self, user_id: uuid.UUID, revoked_at: datetime | None = None
    ) -> int:
        now = revoked_at or datetime.now(UTC)
        count = 0
        for s in self.refresh_sessions.values():
            if s.user_id == user_id and s.revoked_at is None:
                s.revoked_at = now
                count += 1
        return count

    async def update_session_last_used(
        self, session_id: uuid.UUID, last_used_at: datetime | None = None
    ) -> None:
        session = self.refresh_sessions.get(session_id)
        if session:
            session.last_used_at = last_used_at or datetime.now(UTC)

    async def create_verification_token(
        self,
        user_id: uuid.UUID,
        purpose: str,
        token_hash: str,
        expires_at: datetime,
    ) -> VerificationToken:
        token = VerificationToken(
            id=uuid.uuid4(),
            user_id=user_id,
            purpose=purpose,
            token_hash=token_hash,
            expires_at=expires_at,
            consumed_at=None,
            attempt_count=0,
            created_at=datetime.now(UTC),
        )
        self.verification_tokens[token.id] = token
        return token

    async def get_valid_verification_token(
        self, token_hash: str, purpose: str
    ) -> VerificationToken | None:
        now = datetime.now(UTC)
        for t in self.verification_tokens.values():
            if (
                t.token_hash == token_hash
                and t.purpose == purpose
                and t.consumed_at is None
                and t.expires_at > now
            ):
                return t
        return None

    async def get_verification_token_by_hash(
        self, token_hash: str, purpose: str | None = None
    ) -> VerificationToken | None:
        for t in self.verification_tokens.values():
            if t.token_hash == token_hash and (purpose is None or t.purpose == purpose):
                return t
        return None

    async def consume_verification_token(
        self, token_id: uuid.UUID, consumed_at: datetime | None = None
    ) -> bool:
        token = self.verification_tokens.get(token_id)
        if not token:
            return False
        token.consumed_at = consumed_at or datetime.now(UTC)
        return True

    async def increment_token_attempts(self, token_id: uuid.UUID) -> int:
        token = self.verification_tokens.get(token_id)
        if token:
            token.attempt_count += 1
            return token.attempt_count
        return 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def auth_service(fake_repo: FakeUserRepository) -> AuthService:
    return AuthService(user_repo=fake_repo)


@pytest_asyncio.fixture
async def registered_user(fake_repo: FakeUserRepository) -> User:
    """Fixture providing a registered active student."""
    p_hash = hash_password("ValidP@ssw0rd123!")
    return await fake_repo.create_user(
        email="student.reset@university.edu.vn",
        password_hash=p_hash,
        full_name="Student Reset Test",
        role_name="student",
        is_email_verified=True,
    )


@pytest_asyncio.fixture
async def inactive_user(fake_repo: FakeUserRepository) -> User:
    """Fixture providing an inactive user account."""
    p_hash = hash_password("ValidP@ssw0rd123!")
    user = await fake_repo.create_user(
        email="inactive.reset@university.edu.vn",
        password_hash=p_hash,
        full_name="Inactive User",
        role_name="student",
    )
    user.is_active = False
    return user


@pytest_asyncio.fixture
async def client(fake_repo: FakeUserRepository) -> AsyncGenerator[AsyncClient, None]:
    """Test HTTP client with mocked AuthService and FakeUserRepository."""
    service = AuthService(user_repo=fake_repo)

    app.dependency_overrides[get_user_repo] = lambda: fake_repo
    app.dependency_overrides[get_auth_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Repository Layer Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_create_and_get_valid_verification_token(
    fake_repo: FakeUserRepository, registered_user: User
) -> None:
    """Repository creates and retrieves an active, unconsumed token."""
    raw_token = "secret-raw-token-12345"
    token_h = hash_token(raw_token)
    expires = datetime.now(UTC) + timedelta(hours=1)

    created = await fake_repo.create_verification_token(
        user_id=registered_user.id,
        purpose="password_reset",
        token_hash=token_h,
        expires_at=expires,
    )

    assert created.id is not None
    assert created.user_id == registered_user.id
    assert created.purpose == "password_reset"
    assert created.attempt_count == 0
    assert created.consumed_at is None

    # Retrieve valid
    fetched = await fake_repo.get_valid_verification_token(token_h, purpose="password_reset")
    assert fetched is not None
    assert fetched.id == created.id

    # Wrong purpose returns None
    wrong_purpose = await fake_repo.get_valid_verification_token(
        token_h, purpose="email_verification"
    )
    assert wrong_purpose is None


@pytest.mark.asyncio
async def test_repo_consumed_token_is_not_valid(
    fake_repo: FakeUserRepository, registered_user: User
) -> None:
    """A consumed token is no longer returned by get_valid_verification_token."""
    raw_token = "consumed-token-123"
    token_h = hash_token(raw_token)
    expires = datetime.now(UTC) + timedelta(hours=1)

    created = await fake_repo.create_verification_token(
        user_id=registered_user.id,
        purpose="password_reset",
        token_hash=token_h,
        expires_at=expires,
    )

    # Consume it
    consumed = await fake_repo.consume_verification_token(created.id)
    assert consumed is True

    # Check validity
    valid = await fake_repo.get_valid_verification_token(token_h, purpose="password_reset")
    assert valid is None

    # But lookup by hash still works
    by_hash = await fake_repo.get_verification_token_by_hash(token_h)
    assert by_hash is not None
    assert by_hash.consumed_at is not None


@pytest.mark.asyncio
async def test_repo_expired_token_is_not_valid(
    fake_repo: FakeUserRepository, registered_user: User
) -> None:
    """An expired token is not returned by get_valid_verification_token."""
    raw_token = "expired-token-123"
    token_h = hash_token(raw_token)
    past_expiry = datetime.now(UTC) - timedelta(minutes=5)

    await fake_repo.create_verification_token(
        user_id=registered_user.id,
        purpose="password_reset",
        token_hash=token_h,
        expires_at=past_expiry,
    )

    valid = await fake_repo.get_valid_verification_token(token_h, purpose="password_reset")
    assert valid is None


@pytest.mark.asyncio
async def test_repo_increment_token_attempts(
    fake_repo: FakeUserRepository, registered_user: User
) -> None:
    """Attempt count increments properly."""
    raw_token = "attempt-token-123"
    token_h = hash_token(raw_token)
    expires = datetime.now(UTC) + timedelta(hours=1)

    created = await fake_repo.create_verification_token(
        user_id=registered_user.id,
        purpose="password_reset",
        token_hash=token_h,
        expires_at=expires,
    )

    count1 = await fake_repo.increment_token_attempts(created.id)
    assert count1 == 1
    count2 = await fake_repo.increment_token_attempts(created.id)
    assert count2 == 2


@pytest.mark.asyncio
async def test_repo_update_user_password(
    fake_repo: FakeUserRepository, registered_user: User
) -> None:
    """Updating user password updates password_hash and timestamp."""
    new_hash = hash_password("BrandNewP@ssw0rd999!")
    updated = await fake_repo.update_user_password(registered_user.id, new_hash)
    assert updated is True

    user = await fake_repo.get_by_id(registered_user.id)
    assert user is not None
    assert user.password_hash == new_hash


# ---------------------------------------------------------------------------
# Service Layer Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_request_password_reset_existing_user(
    auth_service: AuthService, fake_repo: FakeUserRepository, registered_user: User
) -> None:
    """Requesting password reset for active existing user generates secure token."""
    found, raw_token = await auth_service.request_password_reset(registered_user.email)
    assert found is True
    assert raw_token is not None
    assert len(raw_token) >= 32

    # Check persistence in repo
    token_h = hash_token(raw_token)
    db_token = await fake_repo.get_valid_verification_token(token_h, purpose="password_reset")
    assert db_token is not None
    assert db_token.user_id == registered_user.id


@pytest.mark.asyncio
async def test_service_request_password_reset_non_existent_email(
    auth_service: AuthService, fake_repo: FakeUserRepository
) -> None:
    """Non-existent email returns (False, None) gracefully without revealing user absence."""
    found, raw_token = await auth_service.request_password_reset("nonexistent@university.edu.vn")
    assert found is False
    assert raw_token is None
    assert len(fake_repo.verification_tokens) == 0


@pytest.mark.asyncio
async def test_service_request_password_reset_inactive_user(
    auth_service: AuthService, fake_repo: FakeUserRepository, inactive_user: User
) -> None:
    """Inactive account returns (False, None) without creating a reset token."""
    found, raw_token = await auth_service.request_password_reset(inactive_user.email)
    assert found is False
    assert raw_token is None
    assert len(fake_repo.verification_tokens) == 0


@pytest.mark.asyncio
async def test_service_request_password_reset_empty_email_raises(
    auth_service: AuthService,
) -> None:
    """Empty email raises ValueError."""
    with pytest.raises(ValueError, match="Email cannot be empty"):
        await auth_service.request_password_reset("")


@pytest.mark.asyncio
async def test_service_reset_password_success(
    auth_service: AuthService, fake_repo: FakeUserRepository, registered_user: User
) -> None:
    """Reset password successfully updates password, consumes token, and revokes sessions."""
    # 1. Establish an active refresh session
    refresh_h = hash_token("existing-refresh-token")
    await fake_repo.create_refresh_session(
        user_id=registered_user.id,
        token_hash=refresh_h,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )

    # 2. Request password reset
    _, raw_token = await auth_service.request_password_reset(registered_user.email)
    assert raw_token is not None

    # 3. Perform reset
    new_password = "UpdatedPassword123#!"
    await auth_service.reset_password(token=raw_token, new_password=new_password)

    # 4. Check user password updated
    user = await fake_repo.get_by_id(registered_user.id)
    assert user is not None
    assert verify_password(new_password, user.password_hash) is True
    assert verify_password("ValidP@ssw0rd123!", user.password_hash) is False

    # 5. Check token is consumed
    token_h = hash_token(raw_token)
    assert (
        await fake_repo.get_valid_verification_token(token_h, purpose="password_reset")
        is None
    )

    # 6. Check all refresh sessions revoked
    session = await fake_repo.get_refresh_session_by_hash(refresh_h)
    assert session is not None
    assert session.revoked_at is not None


@pytest.mark.asyncio
async def test_service_reset_password_weak_password_rejected(
    auth_service: AuthService, fake_repo: FakeUserRepository, registered_user: User
) -> None:
    """Weak password raises WeakPasswordError and token is NOT consumed."""
    _, raw_token = await auth_service.request_password_reset(registered_user.email)
    assert raw_token is not None

    with pytest.raises(WeakPasswordError):
        await auth_service.reset_password(token=raw_token, new_password="weak")

    # Token must still be valid for a retry
    token_h = hash_token(raw_token)
    valid_token = await fake_repo.get_valid_verification_token(
        token_h, purpose="password_reset"
    )
    assert valid_token is not None


@pytest.mark.asyncio
async def test_service_reset_password_invalid_or_expired_token(
    auth_service: AuthService, fake_repo: FakeUserRepository, registered_user: User
) -> None:
    """Invalid token raises InvalidTokenError."""
    with pytest.raises(InvalidTokenError):
        await auth_service.reset_password(
            token="completely-bogus-token",
            new_password="StrongP@ssw0rd123!",
        )


# ---------------------------------------------------------------------------
# API Endpoint Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_request_password_reset_existing_user_200(
    client: AsyncClient, registered_user: User
) -> None:
    """Requesting password reset returns 200 with generic message and dev_token in debug mode."""
    response = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": registered_user.email},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "If an account exists with this email" in data["message"]
    # In development/debug mode, dev_token is provided
    assert data.get("dev_token") is not None or data.get("data", {}).get("dev_token") is not None


@pytest.mark.asyncio
async def test_api_request_password_reset_non_existent_email_200(
    client: AsyncClient,
) -> None:
    """Non-existent email returns identical 200 message with no dev_token (prevent enumeration)."""
    response = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "nobody@university.edu.vn"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "If an account exists with this email" in data["message"]
    assert data.get("dev_token") is None
    assert data.get("data", {}).get("dev_token") is None


@pytest.mark.asyncio
async def test_api_request_password_reset_inactive_user_200(
    client: AsyncClient, inactive_user: User
) -> None:
    """Inactive user returns generic 200 message without dev_token."""
    response = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": inactive_user.email},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data.get("dev_token") is None


@pytest.mark.asyncio
async def test_api_request_password_reset_invalid_email_format_422(
    client: AsyncClient,
) -> None:
    """Invalid email format returns 422 Unprocessable Entity."""
    response = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "not-a-valid-email"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_api_confirm_password_reset_end_to_end_flow(
    client: AsyncClient, registered_user: User
) -> None:
    """Full lifecycle: Login -> Request Reset -> Confirm Reset -> Old credentials fail -> New credentials succeed."""
    # 1. Login with initial password
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": registered_user.email,
            "password": "ValidP@ssw0rd123!",
        },
    )
    assert login_resp.status_code == 200
    old_refresh_token = login_resp.json()["data"]["refresh_token"]

    # 2. Request password reset
    req_resp = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": registered_user.email},
    )
    assert req_resp.status_code == 200
    dev_token = req_resp.json().get("dev_token") or req_resp.json().get("data", {}).get("dev_token")
    assert dev_token is not None

    # 3. Confirm password reset with new strong password
    new_password = "BrandNewSuperP@ssw0rd2026!"
    confirm_resp = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": dev_token,
            "new_password": new_password,
        },
    )
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["success"] is True

    # 4. Attempt login with OLD password -> FAILS (401)
    old_login_resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": registered_user.email,
            "password": "ValidP@ssw0rd123!",
        },
    )
    assert old_login_resp.status_code == 401

    # 5. Attempt refresh with OLD refresh token -> FAILS (401 session revoked)
    refresh_resp = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": old_refresh_token},
    )
    assert refresh_resp.status_code == 401

    # 6. Attempt login with NEW password -> SUCCEEDS (200)
    new_login_resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": registered_user.email,
            "password": new_password,
        },
    )
    assert new_login_resp.status_code == 200
    assert new_login_resp.json()["data"]["access_token"] is not None


@pytest.mark.asyncio
async def test_api_confirm_password_reset_weak_password_400(
    client: AsyncClient, registered_user: User
) -> None:
    """Weak password rejected with 400 Bad Request."""
    req_resp = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": registered_user.email},
    )
    dev_token = req_resp.json().get("dev_token") or req_resp.json().get("data", {}).get("dev_token")

    confirm_resp = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": dev_token,
            "new_password": "weak",
        },
    )
    assert confirm_resp.status_code == 400
    body = confirm_resp.json()
    error_msg = body.get("error", {}).get("message") or body.get("detail", "")
    assert "Password does not meet security requirements" in error_msg


@pytest.mark.asyncio
async def test_api_confirm_password_reset_single_use_token_400(
    client: AsyncClient, registered_user: User
) -> None:
    """Token cannot be used twice (single-use guarantee)."""
    # 1. Request reset
    req_resp = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": registered_user.email},
    )
    dev_token = req_resp.json().get("dev_token") or req_resp.json().get("data", {}).get("dev_token")

    # 2. First confirm -> 200
    first_resp = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": dev_token,
            "new_password": "FirstNewP@ssw0rd123!",
        },
    )
    assert first_resp.status_code == 200

    # 3. Second confirm with same token -> 400
    second_resp = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": dev_token,
            "new_password": "SecondNewP@ssw0rd123!",
        },
    )
    assert second_resp.status_code == 400
    body = second_resp.json()
    error_msg = body.get("error", {}).get("message") or body.get("detail", "")
    assert "Invalid or expired password reset token" in error_msg


@pytest.mark.asyncio
async def test_api_confirm_password_reset_expired_token_400(
    client: AsyncClient, fake_repo: FakeUserRepository, registered_user: User
) -> None:
    """Expired token returns 400 Bad Request."""
    raw_token = "expired-token-for-api"
    token_h = hash_token(raw_token)
    past_expiry = datetime.now(UTC) - timedelta(hours=2)

    await fake_repo.create_verification_token(
        user_id=registered_user.id,
        purpose="password_reset",
        token_hash=token_h,
        expires_at=past_expiry,
    )

    confirm_resp = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": raw_token,
            "new_password": "ValidP@ssw0rdNew123!",
        },
    )
    assert confirm_resp.status_code == 400
    body = confirm_resp.json()
    error_msg = body.get("error", {}).get("message") or body.get("detail", "")
    assert "Invalid or expired password reset token" in error_msg


@pytest.mark.asyncio
async def test_api_confirm_password_reset_tampered_token_400(
    client: AsyncClient,
) -> None:
    """Tampered or non-existent token returns 400 Bad Request."""
    confirm_resp = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": "tampered-token-does-not-exist",
            "new_password": "ValidP@ssw0rdNew123!",
        },
    )
    assert confirm_resp.status_code == 400
    body = confirm_resp.json()
    error_msg = body.get("error", {}).get("message") or body.get("detail", "")
    assert "Invalid or expired password reset token" in error_msg


@pytest.mark.asyncio
async def test_api_confirm_password_reset_empty_fields_422(
    client: AsyncClient,
) -> None:
    """Empty token or password returns 422 Unprocessable Entity."""
    resp1 = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": "", "new_password": "ValidP@ssw0rd123!"},
    )
    assert resp1.status_code == 422

    resp2 = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": "some-token", "new_password": ""},
    )
    assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_password_reset_revokes_all_active_sessions_across_devices(
    client: AsyncClient, fake_repo: FakeUserRepository, registered_user: User
) -> None:
    """Crucial security invariant: All active refresh sessions across devices are revoked."""
    # Create 3 active refresh sessions (representing phone, laptop, tablet)
    session_tokens = ["session_phone", "session_laptop", "session_tablet"]
    for raw_s in session_tokens:
        s_hash = hash_token(raw_s)
        await fake_repo.create_refresh_session(
            user_id=registered_user.id,
            token_hash=s_hash,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )

    # Request and confirm password reset
    req_resp = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": registered_user.email},
    )
    dev_token = req_resp.json().get("dev_token") or req_resp.json().get("data", {}).get("dev_token")

    confirm_resp = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": dev_token,
            "new_password": "NewUniversalP@ssw0rd1!",
        },
    )
    assert confirm_resp.status_code == 200

    # Verify all 3 sessions are now revoked
    for raw_s in session_tokens:
        s_hash = hash_token(raw_s)
        session = await fake_repo.get_refresh_session_by_hash(s_hash)
        assert session is not None
        assert session.revoked_at is not None, f"Session {raw_s} was not revoked!"


@pytest.mark.asyncio
async def test_api_request_password_reset_debug_false_hides_token(
    client: AsyncClient, registered_user: User
) -> None:
    """When settings.debug is False, dev_token is omitted from response even for valid user."""
    with patch.object(settings, "debug", False):
        response = await client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": registered_user.email},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data.get("dev_token") is None
        assert data.get("data", {}).get("dev_token") is None
