"""Comprehensive tests for Google OAuth2 Social Login Integration (TASK-M1-07 / US 2.1).

Covers:
1. Google OAuth 2.0 URL generation (scopes, client_id, redirect_uri, state param).
2. Successful authentication & auto-provisioning of new students with verified email.
3. Existing user authentication, email verification upgrade, and avatar sync.
4. Inactive user rejection with HTTP 403 Forbidden.
5. Google token exchange failures returning HTTP 400 with clean error envelope.
6. Unverified email rejection from Google OAuth profile.
7. Input validation (empty/missing code) returning HTTP 422.
"""

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, quote, urlparse

# Ensure backend root is on sys.path for direct execution
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.auth import router as auth_router
from app.core.config import settings
from app.core.security import hash_password
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
    AuthServiceError,
    InactiveUserError,
    generate_google_auth_url,
    get_auth_service,
)

# Ensure auth router is mounted for test runs
if not any(getattr(route, "path", "").startswith("/api/v1/auth") for route in app.routes):
    app.include_router(auth_router)


# ---------------------------------------------------------------------------
# Test Double: In-Memory FakeUserRepository with OAuth support
# ---------------------------------------------------------------------------


class FakeSocialUserRepository(UserRepository):
    """In-memory UserRepository test double for isolated social auth testing."""

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
            avatar_url=avatar_url,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        user.profile = profile
        user.roles = [role]
        self.users[user_id] = user
        return user

    async def create_oauth_user(
        self,
        email: str,
        password_hash: str,
        full_name: str,
        avatar_url: str | None = None,
        role_name: str = "student",
        current_plan_code: str = "free",
    ) -> User:
        return await self.create_user(
            email=email,
            password_hash=password_hash,
            full_name=full_name,
            role_name=role_name,
            current_plan_code=current_plan_code,
            is_email_verified=True,
            avatar_url=avatar_url,
        )

    async def mark_email_verified(self, user_id: uuid.UUID) -> None:
        user = self.users.get(user_id)
        if user:
            user.is_email_verified = True

    async def update_profile_avatar(self, user_id: uuid.UUID, avatar_url: str) -> None:
        user = self.users.get(user_id)
        if user and user.profile:
            user.profile.avatar_url = avatar_url

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

    async def get_refresh_session_by_hash(self, token_hash: str) -> RefreshSession | None:
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo() -> FakeSocialUserRepository:
    return FakeSocialUserRepository()


@pytest.fixture
def auth_service(fake_repo: FakeSocialUserRepository) -> AuthService:
    return AuthService(user_repo=fake_repo)


@pytest_asyncio.fixture
async def existing_user(fake_repo: FakeSocialUserRepository) -> User:
    """Pre-create a student user with unverified email and no avatar."""
    return await fake_repo.create_user(
        email="existing.student@university.edu",
        password_hash=hash_password("ExistingSecret123!"),
        full_name="Existing Student",
        is_email_verified=False,
        avatar_url=None,
    )


@pytest_asyncio.fixture
async def inactive_user(fake_repo: FakeSocialUserRepository) -> User:
    """Pre-create a deactivated user."""
    user = await fake_repo.create_user(
        email="inactive.student@university.edu",
        password_hash=hash_password("InactiveSecret123!"),
        full_name="Inactive Student",
        is_email_verified=True,
    )
    user.is_active = False
    return user


@pytest_asyncio.fixture
async def client(fake_repo: FakeSocialUserRepository) -> AsyncGenerator[AsyncClient, None]:
    """Test client configured with mocked dependency injection."""
    service = AuthService(user_repo=fake_repo)
    app.dependency_overrides[get_user_repo] = lambda: fake_repo
    app.dependency_overrides[get_auth_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper: Create mock httpx client for Google OAuth endpoints
# ---------------------------------------------------------------------------


def create_mock_google_client(
    token_status: int = 200,
    token_payload: dict | None = None,
    userinfo_status: int = 200,
    userinfo_payload: dict | None = None,
) -> AsyncMock:
    """Build an AsyncMock simulating httpx.AsyncClient for Google token & userinfo endpoints."""
    mock_client = AsyncMock()

    # Token response
    default_token_payload = {
        "access_token": "mock-google-access-token-xyz",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile",
    }
    token_resp = MagicMock()
    token_resp.status_code = token_status
    token_resp.json.return_value = (
        token_payload if token_payload is not None else default_token_payload
    )
    token_resp.text = "Mock token response"

    # Userinfo response
    default_userinfo_payload = {
        "sub": "google-sub-12345",
        "email": "new.student@university.edu",
        "email_verified": True,
        "name": "Jane Google Student",
        "picture": "https://lh3.googleusercontent.com/avatar.jpg",
    }
    userinfo_resp = MagicMock()
    userinfo_resp.status_code = userinfo_status
    userinfo_resp.json.return_value = (
        userinfo_payload if userinfo_payload is not None else default_userinfo_payload
    )
    userinfo_resp.text = "Mock userinfo response"

    mock_client.post.return_value = token_resp
    mock_client.get.return_value = userinfo_resp

    return mock_client


# ---------------------------------------------------------------------------
# 1. URL Generation Unit Tests
# ---------------------------------------------------------------------------


def test_generate_google_auth_url_contains_required_components() -> None:
    """Verify that Google OAuth URL contains client_id, redirect_uri, and required scopes."""
    url = generate_google_auth_url()

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "response_type=code" in url
    assert f"client_id={settings.google_client_id}" in url
    assert quote(settings.google_redirect_uri, safe="") in url
    assert "scope=openid%20email%20profile" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=" not in url

    # Parse query parameters to verify accurate decoded values
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    assert query_params["response_type"] == ["code"]
    assert query_params["client_id"] == [settings.google_client_id]
    assert query_params["redirect_uri"] == [settings.google_redirect_uri]
    assert query_params["scope"] == ["openid email profile"]
    assert query_params["access_type"] == ["offline"]
    assert query_params["prompt"] == ["consent"]


def test_generate_google_auth_url_with_state() -> None:
    """Verify state parameter is properly encoded when provided."""
    custom_state = "csrf-security-token-987"
    url = generate_google_auth_url(state=custom_state)

    assert f"state={custom_state}" in url


def test_generate_google_auth_url_service_method(auth_service: AuthService) -> None:
    """Verify AuthService method generates identical URL to standalone function."""
    url = auth_service.generate_google_auth_url(state="test-state")
    assert "state=test-state" in url
    assert f"client_id={settings.google_client_id}" in url


def test_generate_google_auth_url_missing_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify AuthServiceError is raised if GOOGLE_CLIENT_ID is not configured."""
    monkeypatch.setattr(settings, "google_client_id", None)
    with pytest.raises(AuthServiceError, match="Google OAuth client configuration is missing"):
        generate_google_auth_url()


# ---------------------------------------------------------------------------
# 2. Service Layer Google OAuth Authentication Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_google_user_new_user_provisioning(
    auth_service: AuthService, fake_repo: FakeSocialUserRepository
) -> None:
    """Verify that a new user is automatically provisioned with verified email and student role."""
    mock_client = create_mock_google_client(
        userinfo_payload={
            "sub": "1001",
            "email": "fresh.student@university.edu",
            "email_verified": True,
            "name": "Fresh Student",
            "picture": "https://lh3.googleusercontent.com/fresh.jpg",
        }
    )

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        token_dto = await auth_service.authenticate_google_user(code="valid-auth-code")

    assert token_dto.access_token is not None
    assert token_dto.refresh_token is not None
    assert token_dto.token_type == "bearer"
    assert token_dto.user.email == "fresh.student@university.edu"
    assert token_dto.user.full_name == "Fresh Student"
    assert token_dto.user.role == "student"
    assert token_dto.user.is_email_verified is True
    assert token_dto.user.avatar_url == "https://lh3.googleusercontent.com/fresh.jpg"

    # Verify user exists in repository
    persisted_user = await fake_repo.get_by_email("fresh.student@university.edu")
    assert persisted_user is not None
    assert persisted_user.is_email_verified is True
    assert persisted_user.last_login_at is not None
    assert persisted_user.profile.avatar_url == "https://lh3.googleusercontent.com/fresh.jpg"

    # Verify refresh session was persisted
    assert len(fake_repo.refresh_sessions) == 1


@pytest.mark.asyncio
async def test_authenticate_google_user_existing_user_upgrade(
    auth_service: AuthService,
    fake_repo: FakeSocialUserRepository,
    existing_user: User,
) -> None:
    """Verify existing unverified user has email verified and avatar updated upon Google login."""
    assert existing_user.is_email_verified is False
    assert existing_user.profile.avatar_url is None

    mock_client = create_mock_google_client(
        userinfo_payload={
            "sub": "1002",
            "email": existing_user.email,
            "email_verified": True,
            "name": "Existing Student",
            "picture": "https://lh3.googleusercontent.com/existing.jpg",
        }
    )

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        token_dto = await auth_service.authenticate_google_user(code="valid-code")

    assert token_dto.user.id == existing_user.id
    assert token_dto.user.is_email_verified is True
    assert token_dto.user.avatar_url == "https://lh3.googleusercontent.com/existing.jpg"

    # Verify repo state was updated
    assert existing_user.is_email_verified is True
    assert existing_user.profile.avatar_url == "https://lh3.googleusercontent.com/existing.jpg"
    assert existing_user.last_login_at is not None


@pytest.mark.asyncio
async def test_authenticate_google_user_inactive_account_rejected(
    auth_service: AuthService, inactive_user: User
) -> None:
    """Verify deactivated account raises InactiveUserError."""
    mock_client = create_mock_google_client(
        userinfo_payload={
            "sub": "1003",
            "email": inactive_user.email,
            "email_verified": True,
            "name": "Inactive Student",
        }
    )

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        with pytest.raises(InactiveUserError, match="inactive"):
            await auth_service.authenticate_google_user(code="valid-code")


@pytest.mark.asyncio
async def test_authenticate_google_user_unverified_email_rejected(
    auth_service: AuthService,
) -> None:
    """Verify Google profile with email_verified=False is rejected with AuthServiceError."""
    mock_client = create_mock_google_client(
        userinfo_payload={
            "sub": "1004",
            "email": "unverified@university.edu",
            "email_verified": False,
            "name": "Unverified Student",
        }
    )

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        with pytest.raises(AuthServiceError, match="not verified"):
            await auth_service.authenticate_google_user(code="valid-code")


@pytest.mark.asyncio
async def test_authenticate_google_user_token_exchange_failure(
    auth_service: AuthService,
) -> None:
    """Verify Google token endpoint 400 error raises AuthServiceError."""
    mock_client = create_mock_google_client(
        token_status=400,
        token_payload={"error": "invalid_grant", "error_description": "Code expired"},
    )

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        with pytest.raises(AuthServiceError, match="Code expired"):
            await auth_service.authenticate_google_user(code="expired-code")


@pytest.mark.asyncio
async def test_authenticate_google_user_network_error(auth_service: AuthService) -> None:
    """Verify network communication failures raise clean AuthServiceError."""
    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("Failed to connect to Google")

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        with pytest.raises(AuthServiceError, match="Token request failed"):
            await auth_service.authenticate_google_user(code="some-code")


# ---------------------------------------------------------------------------
# 3. API Integration Endpoints Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_get_google_url_success(client: AsyncClient) -> None:
    """GET /api/v1/auth/google/url returns 200 with valid authorization URL."""
    response = await client.get("/api/v1/auth/google/url")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert "url" in payload["data"]
    url = payload["data"]["url"]
    assert "https://accounts.google.com/o/oauth2/v2/auth?" in url
    assert f"client_id={settings.google_client_id}" in url
    assert "scope=openid%20email%20profile" in url


@pytest.mark.asyncio
async def test_api_get_google_url_with_state(client: AsyncClient) -> None:
    """GET /api/v1/auth/google/url?state=test_csrf propagates state into URL."""
    response = await client.get("/api/v1/auth/google/url?state=test_csrf_token_123")

    assert response.status_code == 200
    payload = response.json()
    assert "state=test_csrf_token_123" in payload["data"]["url"]


@pytest.mark.asyncio
async def test_api_google_callback_new_user_success_200(
    client: AsyncClient, fake_repo: FakeSocialUserRepository
) -> None:
    """POST /api/v1/auth/google/callback provisions new user and returns JWT pair."""
    mock_client = create_mock_google_client(
        userinfo_payload={
            "sub": "2001",
            "email": "google.newbie@university.edu",
            "email_verified": True,
            "name": "Google Newbie",
            "picture": "https://example.com/avatar.png",
        }
    )

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        response = await client.post(
            "/api/v1/auth/google/callback",
            json={"code": "valid-auth-code-123", "state": "valid-state"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["message"] == "Login successful."
    assert "access_token" in data["data"]
    assert "refresh_token" in data["data"]
    assert data["data"]["token_type"] == "bearer"

    user_info = data["data"]["user"]
    assert user_info["email"] == "google.newbie@university.edu"
    assert user_info["full_name"] == "Google Newbie"
    assert user_info["role"] == "student"
    assert user_info["is_email_verified"] is True
    assert user_info["avatar_url"] == "https://example.com/avatar.png"


@pytest.mark.asyncio
async def test_api_google_callback_existing_user_success_200(
    client: AsyncClient, existing_user: User
) -> None:
    """POST /api/v1/auth/google/callback logs in existing user and verifies email."""
    mock_client = create_mock_google_client(
        userinfo_payload={
            "sub": "2002",
            "email": existing_user.email,
            "email_verified": True,
            "name": "Existing User",
            "picture": "https://example.com/avatar2.png",
        }
    )

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        response = await client.post(
            "/api/v1/auth/google/callback",
            json={"code": "valid-auth-code"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["user"]["email"] == existing_user.email
    assert data["data"]["user"]["is_email_verified"] is True


@pytest.mark.asyncio
async def test_api_google_callback_inactive_user_forbidden_403(
    client: AsyncClient, inactive_user: User
) -> None:
    """POST /api/v1/auth/google/callback returns HTTP 403 Forbidden for inactive account."""
    mock_client = create_mock_google_client(
        userinfo_payload={
            "sub": "2003",
            "email": inactive_user.email,
            "email_verified": True,
            "name": "Inactive User",
        }
    )

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        response = await client.post(
            "/api/v1/auth/google/callback",
            json={"code": "valid-auth-code"},
        )

    assert response.status_code == 403
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "HTTP_403"
    assert "inactive" in payload["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_google_callback_token_error_bad_request_400(client: AsyncClient) -> None:
    """POST /api/v1/auth/google/callback returns HTTP 400 when Google rejects authorization code."""
    mock_client = create_mock_google_client(
        token_status=400,
        token_payload={"error": "invalid_grant", "error_description": "Malformed auth code"},
    )

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        response = await client.post(
            "/api/v1/auth/google/callback",
            json={"code": "bad-code"},
        )

    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "HTTP_400"
    assert "Malformed auth code" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_api_google_callback_unverified_email_bad_request_400(client: AsyncClient) -> None:
    """POST /api/v1/auth/google/callback returns HTTP 400 when Google email is unverified."""
    mock_client = create_mock_google_client(
        userinfo_payload={
            "sub": "2004",
            "email": "not.verified@example.com",
            "email_verified": False,
            "name": "Unverified User",
        }
    )

    with patch("httpx.AsyncClient") as mock_async_client_cls:
        mock_async_client_cls.return_value.__aenter__.return_value = mock_client

        response = await client.post(
            "/api/v1/auth/google/callback",
            json={"code": "valid-code"},
        )

    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "HTTP_400"
    assert "not verified" in payload["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_google_callback_empty_code_validation_422(client: AsyncClient) -> None:
    """POST /api/v1/auth/google/callback returns HTTP 422 when code is empty."""
    response = await client.post(
        "/api/v1/auth/google/callback",
        json={"code": ""},
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_api_google_callback_missing_body_422(client: AsyncClient) -> None:
    """POST /api/v1/auth/google/callback returns HTTP 422 when body is missing."""
    response = await client.post("/api/v1/auth/google/callback", json={})

    assert response.status_code == 422
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "VALIDATION_ERROR"
