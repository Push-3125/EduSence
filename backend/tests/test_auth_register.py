"""Comprehensive tests for user registration: Repository, Service, and API endpoint (US 2.1)."""

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
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
from app.core.security import verify_password
from app.main import app
from app.repositories.user_repo import Role, User, UserProfile, UserRepository
from app.services.auth_service import (
    AuthService,
    UserAlreadyExistsError,
    UserDTO,
    WeakPasswordError,
    get_auth_service,
)

# Ensure auth router is mounted on app for test runs
if not any(getattr(route, "path", "").startswith("/api/v1/auth") for route in app.routes):
    app.include_router(auth_router)


# ---------------------------------------------------------------------------
# Test Fixtures & In-Memory Fake Repository
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository test double for isolated unit and endpoint testing."""

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


@pytest.fixture
def fake_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def auth_service(fake_repo: FakeUserRepository) -> AuthService:
    return AuthService(user_repo=fake_repo)


@pytest_asyncio.fixture
async def client(fake_repo: FakeUserRepository) -> AsyncGenerator[AsyncClient, None]:
    """Test HTTP client with mocked AuthService wired to in-memory fake repo."""
    service = AuthService(user_repo=fake_repo)
    app.dependency_overrides[get_auth_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Repository Layer Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_repository_create_and_fetch() -> None:
    """Verify UserRepository correctly constructs models and flushes associations."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()

    # Mock get_role_by_name returning student role
    role = Role(id=uuid.uuid4(), name="student")
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = role
    mock_session.execute.return_value = mock_result

    repo = UserRepository(session=mock_session)
    user = await repo.create_user(
        email="test@university.edu",
        password_hash="$argon2id$v=19$testhash",
        full_name="Alice Student",
        role_name="student",
    )

    assert user.email == "test@university.edu"
    assert user.password_hash == "$argon2id$v=19$testhash"
    assert user.current_plan_code == "free"
    assert user.is_active is True
    assert user.is_email_verified is False
    assert user.profile.full_name == "Alice Student"
    assert len(user.roles) == 1
    assert user.roles[0].name == "student"

    # Verify session additions
    assert mock_session.add.call_count >= 3  # user, profile, user_role
    assert mock_session.flush.call_count >= 2


@pytest.mark.asyncio
async def test_user_repository_get_by_email_case_insensitivity(fake_repo: FakeUserRepository) -> None:
    """Verify email lookups normalize case and whitespace."""
    await fake_repo.create_user(
        email="student@example.com",
        password_hash="hashed_pw",
        full_name="Test Student",
    )

    found = await fake_repo.get_by_email("  STUDENT@EXAMPLE.COM  ")
    assert found is not None
    assert found.email == "student@example.com"

    not_found = await fake_repo.get_by_email("unknown@example.com")
    assert not_found is None


# ---------------------------------------------------------------------------
# 2. Service Layer Unit Tests (AuthService)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_service_successful_registration(auth_service: AuthService) -> None:
    """Verify successful user registration hashes password with Argon2id and sets defaults."""
    plain_password = "SecurePassword2026!"
    user_dto: UserDTO = await auth_service.register_user(
        email="Jane.Doe@university.edu",
        password=plain_password,
        full_name="Jane Doe",
    )

    assert isinstance(user_dto.id, uuid.UUID)
    assert user_dto.email == "jane.doe@university.edu"
    assert user_dto.full_name == "Jane Doe"
    assert user_dto.role == "student"
    assert user_dto.current_plan_code == "free"
    assert user_dto.is_active is True
    assert user_dto.is_email_verified is False

    # Check persistence in repo and password hash verification
    stored_user = await auth_service.user_repo.get_by_email("jane.doe@university.edu")
    assert stored_user is not None
    assert stored_user.password_hash.startswith("$argon2id$v=19$")
    assert verify_password(plain_password, stored_user.password_hash) is True
    assert verify_password("WrongPassword123!", stored_user.password_hash) is False


@pytest.mark.asyncio
async def test_auth_service_duplicate_email_conflict(auth_service: AuthService) -> None:
    """Verify registering an already registered email raises UserAlreadyExistsError."""
    await auth_service.register_user(
        email="duplicate@university.edu",
        password="ValidPassword123!",
        full_name="First User",
    )

    with pytest.raises(UserAlreadyExistsError, match="already exists"):
        await auth_service.register_user(
            email="DUPLICATE@university.edu",
            password="AnotherPassword456!",
            full_name="Second User",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "weak_password,reason",
    [
        ("short1!", "Password must be at least 8 characters long"),
        ("nouppercase123!", "Password must contain at least one uppercase letter"),
        ("NOLOWERCASE123!", "Password must contain at least one lowercase letter"),
        ("NoDigitsHere!!-Password must contain at least one numeric digit", "Password must contain at least one numeric digit"),
        ("NoSpecialChar123", "Password must contain at least one special character"),
        ("", "Password must be at least 8 characters long"),
    ],
)
async def test_auth_service_weak_passwords_rejected(
    auth_service: AuthService, weak_password: str, reason: str
) -> None:
    """Verify all password policy violations are caught and raise WeakPasswordError."""
    with pytest.raises(WeakPasswordError) as exc_info:
        await auth_service.register_user(
            email="valid@university.edu",
            password=weak_password,
            full_name="Test Student",
        )
    assert any(reason in err for err in exc_info.value.errors) or reason in str(exc_info.value)


@pytest.mark.asyncio
async def test_auth_service_empty_required_fields(auth_service: AuthService) -> None:
    """Verify empty email or full name raises ValueError."""
    with pytest.raises(ValueError, match="Email cannot be empty"):
        await auth_service.register_user(
            email="   ",
            password="ValidPassword123!",
            full_name="Valid Name",
        )

    with pytest.raises(ValueError, match="Full name cannot be empty"):
        await auth_service.register_user(
            email="valid@university.edu",
            password="ValidPassword123!",
            full_name="   ",
        )


# ---------------------------------------------------------------------------
# 3. API Endpoint Integration Tests (POST /api/v1/auth/register)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_register_endpoint_success_201(client: AsyncClient) -> None:
    """Verify POST /api/v1/auth/register returns 201 Created and standard envelope."""
    payload = {
        "email": "Student.Alice@university.edu",
        "password": "StrongStudentPass123!",
        "full_name": "Alice Wonderland",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201

    data = response.json()
    assert data["success"] is True
    assert data["message"] == "User registered successfully."

    user_info = data["data"]
    assert user_info["email"] == "student.alice@university.edu"
    assert user_info["full_name"] == "Alice Wonderland"
    assert user_info["role"] == "student"
    assert user_info["current_plan_code"] == "free"
    assert user_info["is_active"] is True
    assert user_info["is_email_verified"] is False
    assert "id" in user_info
    assert "created_at" in user_info

    # Security check: Password hash or credentials MUST NOT be leaked
    assert "password" not in user_info
    assert "password_hash" not in user_info


@pytest.mark.asyncio
async def test_api_register_duplicate_email_conflict_409(client: AsyncClient) -> None:
    """Verify POST /api/v1/auth/register with registered email returns 409 Conflict."""
    payload = {
        "email": "existing.student@university.edu",
        "password": "StrongStudentPass123!",
        "full_name": "First Account",
    }
    # Initial registration
    res1 = await client.post("/api/v1/auth/register", json=payload)
    assert res1.status_code == 201

    # Duplicate attempt
    res2 = await client.post("/api/v1/auth/register", json=payload)
    assert res2.status_code == 409

    body = res2.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_409"
    assert "already exists" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_register_weak_password_bad_request_400(client: AsyncClient) -> None:
    """Verify POST /api/v1/auth/register with weak password returns 400 Bad Request."""
    payload = {
        "email": "weak.password@university.edu",
        "password": "weak",
        "full_name": "Test User",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 400

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_400"
    assert "password does not meet security requirements" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_register_invalid_email_format_422(client: AsyncClient) -> None:
    """Verify POST /api/v1/auth/register with malformed email returns 422 Unprocessable Entity."""
    payload = {
        "email": "not-an-email-address",
        "password": "StrongStudentPass123!",
        "full_name": "Test User",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 422

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "Request input validation failed." in body["error"]["message"]


@pytest.mark.asyncio
async def test_api_register_whitespace_full_name_422(client: AsyncClient) -> None:
    """Verify POST /api/v1/auth/register with whitespace-only full name returns 422."""
    payload = {
        "email": "valid.student@university.edu",
        "password": "StrongStudentPass123!",
        "full_name": "    ",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 422

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_api_register_missing_fields_422(client: AsyncClient) -> None:
    """Verify POST /api/v1/auth/register with missing required fields returns 422."""
    # Missing full_name
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "student@university.edu", "password": "StrongPassword123!"},
    )
    assert response.status_code == 422

    # Missing password
    response2 = await client.post(
        "/api/v1/auth/register",
        json={"email": "student@university.edu", "full_name": "Student Alice"},
    )
    assert response2.status_code == 422

    # Missing email
    response3 = await client.post(
        "/api/v1/auth/register",
        json={"password": "StrongPassword123!", "full_name": "Student Alice"},
    )
    assert response3.status_code == 422
