"""Comprehensive tests for user login, refresh token session, revocation, and /me endpoint (US 2.2)."""

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

from app.api.v1.auth import router as auth_router
from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_token,
    verify_jwt_token,
)
from app.main import app
from app.repositories.user_repo import (
    RefreshSession,
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.auth_service import (
    AuthService,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidSessionError,
    get_auth_service,
)

# Ensure auth router is mounted on app for test runs
if not any(getattr(route, "path", "").startswith("/api/v1/auth") for route in app.routes):
    app.include_router(auth_router)


# ---------------------------------------------------------------------------
# Test Double: In-Memory FakeUserRepository
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository test double for isolated unit and endpoint testing."""

    def __init__(self) -> None:
        self.users: dict[uuid.UUID, User] = {}
        self.refresh_sessions: dict[uuid.UUID, RefreshSession] = {}
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
            is_email_verified=False,
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

    async def create_refresh_session(
        self,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshSession:
        session_id = uuid.uuid4()
        session = RefreshSession(
            id=session_id,
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            revoked_at=None,
            last_used_at=None,
            user_agent=user_agent,
            ip_address=ip_address,
            created_at=datetime.now(UTC),
        )
        self.refresh_sessions[session_id] = session
        return session

    async def get_refresh_session_by_hash(
        self, token_hash: str
    ) -> RefreshSession | None:
        for s in self.refresh_sessions.values():
            if s.token_hash == token_hash:
                return s
        return None

    async def revoke_refresh_session(
        self,
        token_hash: str,
        revoked_at: datetime | None = None,
    ) -> bool:
        session = await self.get_refresh_session_by_hash(token_hash)
        if not session or session.revoked_at is not None:
            return False
        session.revoked_at = revoked_at or datetime.now(UTC)
        return True

    async def revoke_all_user_sessions(
        self,
        user_id: uuid.UUID,
        revoked_at: datetime | None = None,
    ) -> int:
        now = revoked_at or datetime.now(UTC)
        count = 0
        for s in self.refresh_sessions.values():
            if s.user_id == user_id and s.revoked_at is None:
                s.revoked_at = now
                count += 1
        return count

    async def update_session_last_used(
        self,
        session_id: uuid.UUID,
        last_used_at: datetime | None = None,
    ) -> None:
        session = self.refresh_sessions.get(session_id)
        if session:
            session.last_used_at = last_used_at or datetime.now(UTC)


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
    """Pre-create a registered active student user."""
    password_hash = hash_password("ValidPass123!")
    return await fake_repo.create_user(
        email="test.student@university.edu",
        password_hash=password_hash,
        full_name="Bob Student",
    )


@pytest_asyncio.fixture
async def inactive_user(fake_repo: FakeUserRepository) -> User:
    """Pre-create a deactivated user."""
    password_hash = hash_password("InactivePass123!")
    user = await fake_repo.create_user(
        email="inactive.student@university.edu",
        password_hash=password_hash,
        full_name="Inactive User",
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
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Repository Layer Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_refresh_session_crud() -> None:
    """Verify UserRepository refresh session persistence and revocation."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()

    user_id = uuid.uuid4()
    token_h = hash_token("dummy_refresh_token")
    expires = datetime.now(UTC) + timedelta(days=7)

    repo = UserRepository(session=mock_session)
    session = await repo.create_refresh_session(
        user_id=user_id,
        token_hash=token_h,
        expires_at=expires,
        user_agent="Mozilla/5.0",
        ip_address="192.168.1.1",
    )

    assert session.user_id == user_id
    assert session.token_hash == token_h
    assert session.expires_at == expires
    assert session.user_agent == "Mozilla/5.0"
    assert session.ip_address == "192.168.1.1"
    assert mock_session.add.called
    assert mock_session.flush.called


# ---------------------------------------------------------------------------
# 2. Service Layer Unit Tests (AuthService)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_service_login_success(
    auth_service: AuthService, registered_user: User
) -> None:
    """Verify successful login returns valid JWT access and refresh token pair."""
    token_dto = await auth_service.login_user(
        email=registered_user.email,
        password="ValidPass123!",
        user_agent="TestAgent/1.0",
        ip_address="10.0.0.1",
    )

    assert token_dto.token_type == "bearer"
    assert token_dto.access_token is not None
    assert token_dto.refresh_token is not None
    assert token_dto.user.email == registered_user.email
    assert token_dto.user.full_name == "Bob Student"

    # Verify access token structure and type
    access_payload = verify_jwt_token(token_dto.access_token, expected_type=TOKEN_TYPE_ACCESS)
    assert access_payload["sub"] == str(registered_user.id)
    assert access_payload["role"] == "student"

    # Verify refresh token structure and type
    refresh_payload = verify_jwt_token(token_dto.refresh_token, expected_type=TOKEN_TYPE_REFRESH)
    assert refresh_payload["sub"] == str(registered_user.id)

    # Verify session recorded in repo
    token_h = hash_token(token_dto.refresh_token)
    session = await auth_service.user_repo.get_refresh_session_by_hash(token_h)
    assert session is not None
    assert session.user_id == registered_user.id
    assert session.revoked_at is None


@pytest.mark.asyncio
async def test_auth_service_login_invalid_password(
    auth_service: AuthService, registered_user: User
) -> None:
    """Verify login with incorrect password raises InvalidCredentialsError."""
    with pytest.raises(InvalidCredentialsError, match="Invalid email or password"):
        await auth_service.login_user(
            email=registered_user.email,
            password="WrongPassword123!",
        )


@pytest.mark.asyncio
async def test_auth_service_login_non_existent_email(auth_service: AuthService) -> None:
    """Verify non-existent email raises InvalidCredentialsError in constant time."""
    with pytest.raises(InvalidCredentialsError, match="Invalid email or password"):
        await auth_service.login_user(
            email="nonexistent.user@university.edu",
            password="AnyPassword123!",
        )


@pytest.mark.asyncio
async def test_auth_service_login_inactive_user(
    auth_service: AuthService, inactive_user: User
) -> None:
    """Verify login with inactive account raises InactiveUserError."""
    with pytest.raises(InactiveUserError, match="User account is inactive"):
        await auth_service.login_user(
            email=inactive_user.email,
            password="InactivePass123!",
        )


@pytest.mark.asyncio
async def test_auth_service_login_empty_fields(auth_service: AuthService) -> None:
    """Verify empty email or password raises ValueError."""
    with pytest.raises(ValueError, match="Email and password cannot be empty"):
        await auth_service.login_user(email="", password="SomePassword123!")

    with pytest.raises(ValueError, match="Email and password cannot be empty"):
        await auth_service.login_user(email="test@university.edu", password="")


@pytest.mark.asyncio
async def test_auth_service_refresh_token_success(
    auth_service: AuthService, registered_user: User
) -> None:
    """Verify valid refresh token issues a new access token and updates last_used_at."""
    token_dto = await auth_service.login_user(
        email=registered_user.email,
        password="ValidPass123!",
    )

    new_access_token = await auth_service.refresh_access_token(token_dto.refresh_token)
    assert new_access_token is not None

    payload = verify_jwt_token(new_access_token, expected_type=TOKEN_TYPE_ACCESS)
    assert payload["sub"] == str(registered_user.id)

    # Check last_used_at was updated on the session
    token_h = hash_token(token_dto.refresh_token)
    session = await auth_service.user_repo.get_refresh_session_by_hash(token_h)
    assert session is not None
    assert session.last_used_at is not None


@pytest.mark.asyncio
async def test_auth_service_refresh_token_invalid_jwt(auth_service: AuthService) -> None:
    """Verify malformed or invalid refresh token raises InvalidSessionError."""
    with pytest.raises(InvalidSessionError, match="Invalid refresh token"):
        await auth_service.refresh_access_token("completely-invalid-jwt-token")


@pytest.mark.asyncio
async def test_auth_service_refresh_token_expired_jwt(
    auth_service: AuthService, registered_user: User
) -> None:
    """Verify expired JWT refresh token raises InvalidSessionError."""
    expired_token = create_refresh_token(
        subject=str(registered_user.id),
        expires_delta=timedelta(seconds=-10),
    )
    with pytest.raises(InvalidSessionError, match="Refresh token has expired"):
        await auth_service.refresh_access_token(expired_token)


@pytest.mark.asyncio
async def test_auth_service_refresh_token_revoked_session(
    auth_service: AuthService, registered_user: User
) -> None:
    """Verify revoked refresh session raises InvalidSessionError."""
    token_dto = await auth_service.login_user(
        email=registered_user.email,
        password="ValidPass123!",
    )

    # Revoke session via logout
    await auth_service.logout_user(token_dto.refresh_token)

    with pytest.raises(InvalidSessionError, match="Refresh session has been revoked"):
        await auth_service.refresh_access_token(token_dto.refresh_token)


@pytest.mark.asyncio
async def test_auth_service_refresh_token_expired_db_session(
    auth_service: AuthService, registered_user: User
) -> None:
    """Verify DB session expired timestamp raises InvalidSessionError."""
    token_dto = await auth_service.login_user(
        email=registered_user.email,
        password="ValidPass123!",
    )

    # Manually expire session in DB test double
    token_h = hash_token(token_dto.refresh_token)
    session = await auth_service.user_repo.get_refresh_session_by_hash(token_h)
    assert session is not None
    session.expires_at = datetime.now(UTC) - timedelta(hours=1)

    with pytest.raises(InvalidSessionError, match="Refresh session has expired"):
        await auth_service.refresh_access_token(token_dto.refresh_token)


@pytest.mark.asyncio
async def test_auth_service_logout_revokes_session(
    auth_service: AuthService, registered_user: User
) -> None:
    """Verify logout_user revokes active session and is idempotent."""
    token_dto = await auth_service.login_user(
        email=registered_user.email,
        password="ValidPass123!",
    )

    token_h = hash_token(token_dto.refresh_token)
    session = await auth_service.user_repo.get_refresh_session_by_hash(token_h)
    assert session is not None
    assert session.revoked_at is None

    # First logout revokes
    revoked = await auth_service.logout_user(token_dto.refresh_token)
    assert revoked is True
    assert session.revoked_at is not None

    # Second logout is idempotent and returns False without error
    revoked_again = await auth_service.logout_user(token_dto.refresh_token)
    assert revoked_again is False


# ---------------------------------------------------------------------------
# 3. API Endpoints Integration Tests (POST /login, /refresh, /logout, GET /me)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_login_success_200(
    client: AsyncClient, registered_user: User
) -> None:
    """Verify POST /api/v1/auth/login returns 200 OK with token pair and user profile."""
    payload = {
        "email": registered_user.email,
        "password": "ValidPass123!",
    }
    response = await client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 200

    data = response.json()
    assert data["success"] is True
    assert data["message"] == "Login successful."

    token_data = data["data"]
    assert "access_token" in token_data
    assert "refresh_token" in token_data
    assert token_data["token_type"] == "bearer"

    user_info = token_data["user"]
    assert user_info["email"] == registered_user.email
    assert user_info["full_name"] == "Bob Student"
    assert user_info["role"] == "student"

    # Security check: Password or hashes MUST NOT be exposed
    assert "password" not in user_info
    assert "password_hash" not in user_info


@pytest.mark.asyncio
async def test_api_login_invalid_password_401(
    client: AsyncClient, registered_user: User
) -> None:
    """Verify POST /api/v1/auth/login returns 401 with wrong password."""
    payload = {
        "email": registered_user.email,
        "password": "WrongPassword123!",
    }
    response = await client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 401

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"
    assert "invalid email or password" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_login_non_existent_email_401(client: AsyncClient) -> None:
    """Verify POST /api/v1/auth/login returns 401 with unknown email."""
    payload = {
        "email": "ghost.user@university.edu",
        "password": "AnyPassword123!",
    }
    response = await client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 401

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"
    assert "invalid email or password" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_login_inactive_user_403(
    client: AsyncClient, inactive_user: User
) -> None:
    """Verify POST /api/v1/auth/login returns 403 Forbidden for inactive accounts."""
    payload = {
        "email": inactive_user.email,
        "password": "InactivePass123!",
    }
    response = await client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 403

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_403"
    assert "inactive" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_login_malformed_email_422(client: AsyncClient) -> None:
    """Verify POST /api/v1/auth/login returns 422 for malformed email."""
    payload = {
        "email": "not-a-valid-email",
        "password": "ValidPass123!",
    }
    response = await client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 422

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_api_refresh_token_success_200(
    client: AsyncClient, registered_user: User
) -> None:
    """Verify POST /api/v1/auth/refresh returns 200 OK with new access token."""
    login_res = await client.post(
        "/api/v1/auth/login",
        json={"email": registered_user.email, "password": "ValidPass123!"},
    )
    refresh_token = login_res.json()["data"]["refresh_token"]

    refresh_res = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_res.status_code == 200

    data = refresh_res.json()
    assert data["success"] is True
    assert "access_token" in data["data"]
    assert data["data"]["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_api_refresh_token_invalid_401(client: AsyncClient) -> None:
    """Verify POST /api/v1/auth/refresh with bogus token returns 401."""
    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": "corrupted.token.signature"},
    )
    assert response.status_code == 401

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"


@pytest.mark.asyncio
async def test_api_refresh_token_revoked_session_401(
    client: AsyncClient, registered_user: User
) -> None:
    """Verify POST /api/v1/auth/refresh returns 401 after session is revoked."""
    login_res = await client.post(
        "/api/v1/auth/login",
        json={"email": registered_user.email, "password": "ValidPass123!"},
    )
    refresh_token = login_res.json()["data"]["refresh_token"]

    # Revoke session via logout
    logout_res = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh_token},
    )
    assert logout_res.status_code == 200

    # Attempt to refresh with revoked token
    refresh_res = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_res.status_code == 401

    body = refresh_res.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"
    assert "revoked" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_logout_success_200(
    client: AsyncClient, registered_user: User
) -> None:
    """Verify POST /api/v1/auth/logout returns 200 OK and revokes session."""
    login_res = await client.post(
        "/api/v1/auth/login",
        json={"email": registered_user.email, "password": "ValidPass123!"},
    )
    refresh_token = login_res.json()["data"]["refresh_token"]

    logout_res = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh_token},
    )
    assert logout_res.status_code == 200

    data = logout_res.json()
    assert data["success"] is True
    assert data["message"] == "Logged out successfully."


@pytest.mark.asyncio
async def test_api_get_me_protected_endpoint_success_200(
    client: AsyncClient, registered_user: User
) -> None:
    """Verify GET /api/v1/auth/me returns current user info when valid access token provided."""
    login_res = await client.post(
        "/api/v1/auth/login",
        json={"email": registered_user.email, "password": "ValidPass123!"},
    )
    access_token = login_res.json()["data"]["access_token"]

    headers = {"Authorization": f"Bearer {access_token}"}
    me_res = await client.get("/api/v1/auth/me", headers=headers)
    assert me_res.status_code == 200

    data = me_res.json()
    assert data["success"] is True
    user_info = data["data"]
    assert user_info["email"] == registered_user.email
    assert user_info["full_name"] == "Bob Student"
    assert user_info["role"] == "student"


@pytest.mark.asyncio
async def test_api_get_me_missing_token_401(client: AsyncClient) -> None:
    """Verify GET /api/v1/auth/me without Authorization header returns 401."""
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"
    assert "not authenticated" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_get_me_expired_token_401(
    client: AsyncClient, registered_user: User
) -> None:
    """Verify GET /api/v1/auth/me with expired access token returns 401."""
    expired_access_token = create_access_token(
        subject=str(registered_user.id),
        expires_delta=timedelta(seconds=-10),
    )
    headers = {"Authorization": f"Bearer {expired_access_token}"}
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"
    assert "expired" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_get_me_invalid_token_401(client: AsyncClient) -> None:
    """Verify GET /api/v1/auth/me with invalid/corrupted token returns 401."""
    headers = {"Authorization": "Bearer invalid.jwt.string"}
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"
    assert "invalid token" in body["error"]["message"].lower()
