"""Comprehensive tests for user profile CRUD and student information management (US 2.4)."""

import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure backend root is on sys.path for direct execution
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.v1.users import router as users_router
from app.core.security import create_access_token, hash_password
from app.main import app
from app.repositories.user_repo import (
    Role,
    User,
    UserProfile,
    UserRepository,
    get_user_repo,
)
from app.services.user_service import (
    NotFoundError,
    ProfileDTO,
    ProfileValidationError,
    UserService,
    get_user_service,
)

# Ensure users router is mounted on app
if not any(getattr(route, "path", "").startswith("/api/v1/users") for route in app.routes):
    app.include_router(users_router)


# ---------------------------------------------------------------------------
# Test Double: In-Memory FakeUserRepository
# ---------------------------------------------------------------------------


class FakeUserRepository(UserRepository):
    """In-memory UserRepository double for isolated profile testing."""

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
            display_name=None,
            avatar_url=avatar_url,
            phone_number=None,
            university=None,
            major=None,
            academic_year=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        user.profile = profile
        profile.user = user
        user.roles = [role]
        self.users[user_id] = user
        return user

    async def get_profile_by_user_id(
        self, user_id: uuid.UUID
    ) -> UserProfile | None:
        user = self.users.get(user_id)
        if user and user.profile:
            user.profile.user = user
            return user.profile
        return None

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
        user.profile.user = user
        return user.profile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def user_service(fake_repo: FakeUserRepository) -> UserService:
    return UserService(user_repo=fake_repo)


@pytest_asyncio.fixture
async def active_student(fake_repo: FakeUserRepository) -> User:
    """Pre-create an active student with initial profile."""
    password_hash = hash_password("ValidPass123!")
    return await fake_repo.create_user(
        email="alice.student@university.edu",
        password_hash=password_hash,
        full_name="Alice Student",
    )


@pytest_asyncio.fixture
async def inactive_student(fake_repo: FakeUserRepository) -> User:
    """Pre-create a deactivated student."""
    password_hash = hash_password("ValidPass123!")
    user = await fake_repo.create_user(
        email="inactive.student@university.edu",
        password_hash=password_hash,
        full_name="Inactive Student",
    )
    user.is_active = False
    return user


@pytest_asyncio.fixture
async def client(fake_repo: FakeUserRepository) -> AsyncGenerator[AsyncClient, None]:
    """Test HTTP client with dependency overrides for UserRepository and UserService."""
    service = UserService(user_repo=fake_repo)
    app.dependency_overrides[get_user_repo] = lambda: fake_repo
    app.dependency_overrides[get_user_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_auth_headers(user_id: uuid.UUID, expires_seconds: int = 3600) -> dict[str, str]:
    """Helper to generate Authorization header with access token."""
    token = create_access_token(
        subject=str(user_id),
        expires_delta=timedelta(seconds=expires_seconds),
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Repository Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_get_profile_by_user_id_existing(
    fake_repo: FakeUserRepository, active_student: User
) -> None:
    """Verify UserRepository.get_profile_by_user_id returns user profile."""
    profile = await fake_repo.get_profile_by_user_id(active_student.id)
    assert profile is not None
    assert profile.user_id == active_student.id
    assert profile.full_name == "Alice Student"


@pytest.mark.asyncio
async def test_repo_get_profile_by_user_id_nonexistent(
    fake_repo: FakeUserRepository,
) -> None:
    """Verify UserRepository.get_profile_by_user_id returns None for unknown ID."""
    profile = await fake_repo.get_profile_by_user_id(uuid.uuid4())
    assert profile is None


@pytest.mark.asyncio
async def test_repo_update_profile_allowed_fields(
    fake_repo: FakeUserRepository, active_student: User
) -> None:
    """Verify UserRepository.update_profile updates all allowed fields."""
    updated = await fake_repo.update_profile(
        active_student.id,
        full_name="Alice Smith",
        display_name="Ali",
        phone_number="+84987654321",
        university="HCMUT",
        major="Computer Science",
        academic_year=3,
        avatar_url="https://storage.edusense.io/avatars/ali.png",
    )
    assert updated is not None
    assert updated.full_name == "Alice Smith"
    assert updated.display_name == "Ali"
    assert updated.phone_number == "+84987654321"
    assert updated.university == "HCMUT"
    assert updated.major == "Computer Science"
    assert updated.academic_year == 3
    assert updated.avatar_url == "https://storage.edusense.io/avatars/ali.png"


@pytest.mark.asyncio
async def test_repo_update_profile_ignores_disallowed_fields(
    fake_repo: FakeUserRepository, active_student: User
) -> None:
    """Verify UserRepository.update_profile does not modify primary key or user_id."""
    original_id = active_student.profile.id
    bogus_id = uuid.uuid4()
    await fake_repo.update_profile(active_student.id, id=bogus_id, is_admin=True)
    profile = await fake_repo.get_profile_by_user_id(active_student.id)
    assert profile is not None
    assert profile.id == original_id
    assert not hasattr(profile, "is_admin")


# ---------------------------------------------------------------------------
# 2. UserService Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_service_get_profile_success(
    user_service: UserService, active_student: User
) -> None:
    """Verify UserService.get_profile returns populated ProfileDTO."""
    dto = await user_service.get_profile(active_student.id)
    assert isinstance(dto, ProfileDTO)
    assert dto.user_id == active_student.id
    assert dto.email == active_student.email
    assert dto.full_name == "Alice Student"


@pytest.mark.asyncio
async def test_user_service_get_profile_not_found(
    user_service: UserService,
) -> None:
    """Verify UserService.get_profile raises NotFoundError when profile is missing."""
    with pytest.raises(NotFoundError, match="Profile for user .* not found"):
        await user_service.get_profile(uuid.uuid4())


@pytest.mark.asyncio
async def test_user_service_update_profile_success(
    user_service: UserService, active_student: User
) -> None:
    """Verify UserService.update_profile updates profile fields and returns ProfileDTO."""
    dto = await user_service.update_profile(
        active_student.id,
        full_name="Alice Wonder",
        university="Vietnam National University",
        major="Software Engineering",
        academic_year=4,
    )
    assert dto.full_name == "Alice Wonder"
    assert dto.university == "Vietnam National University"
    assert dto.major == "Software Engineering"
    assert dto.academic_year == 4


@pytest.mark.asyncio
async def test_user_service_update_profile_invalid_academic_year_below_range(
    user_service: UserService, active_student: User
) -> None:
    """Verify UserService.update_profile rejects academic_year < 1."""
    with pytest.raises(ProfileValidationError, match="Academic year must be an integer between 1 and 10"):
        await user_service.update_profile(active_student.id, academic_year=0)


@pytest.mark.asyncio
async def test_user_service_update_profile_invalid_academic_year_above_range(
    user_service: UserService, active_student: User
) -> None:
    """Verify UserService.update_profile rejects academic_year > 10."""
    with pytest.raises(ProfileValidationError, match="Academic year must be an integer between 1 and 10"):
        await user_service.update_profile(active_student.id, academic_year=15)


@pytest.mark.asyncio
async def test_user_service_update_profile_whitespace_full_name(
    user_service: UserService, active_student: User
) -> None:
    """Verify UserService.update_profile rejects full_name with only whitespace."""
    with pytest.raises(ProfileValidationError, match="Full name cannot be empty or whitespace"):
        await user_service.update_profile(active_student.id, full_name="    ")


@pytest.mark.asyncio
async def test_user_service_update_profile_not_found(
    user_service: UserService,
) -> None:
    """Verify UserService.update_profile raises NotFoundError if user not found."""
    with pytest.raises(NotFoundError):
        await user_service.update_profile(uuid.uuid4(), full_name="Nobody")


# ---------------------------------------------------------------------------
# 3. API Integration Tests (GET, PUT, PATCH /api/v1/users/profile/me)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_get_profile_me_success_200(
    client: AsyncClient, active_student: User
) -> None:
    """Verify GET /api/v1/users/profile/me with valid Bearer token returns 200 OK."""
    headers = make_auth_headers(active_student.id)
    response = await client.get("/api/v1/users/profile/me", headers=headers)
    assert response.status_code == 200

    body = response.json()
    assert body["success"] is True
    assert body["message"] == "User profile retrieved successfully."

    data = body["data"]
    assert data["user_id"] == str(active_student.id)
    assert data["email"] == active_student.email
    assert data["full_name"] == "Alice Student"
    assert data["academic_year"] is None

    # Security check: password hashes must never be exposed
    assert "password" not in data
    assert "password_hash" not in data


@pytest.mark.asyncio
async def test_api_get_profile_me_unauthenticated_401(client: AsyncClient) -> None:
    """Verify GET /api/v1/users/profile/me without Authorization header returns 401."""
    response = await client.get("/api/v1/users/profile/me")
    assert response.status_code == 401

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"
    assert "not authenticated" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_get_profile_me_expired_token_401(
    client: AsyncClient, active_student: User
) -> None:
    """Verify GET /api/v1/users/profile/me with expired token returns 401."""
    headers = make_auth_headers(active_student.id, expires_seconds=-30)
    response = await client.get("/api/v1/users/profile/me", headers=headers)
    assert response.status_code == 401

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"
    assert "expired" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_get_profile_me_invalid_token_401(client: AsyncClient) -> None:
    """Verify GET /api/v1/users/profile/me with malformed token returns 401."""
    headers = {"Authorization": "Bearer not-a-valid-jwt-token"}
    response = await client.get("/api/v1/users/profile/me", headers=headers)
    assert response.status_code == 401

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"
    assert "invalid token" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_get_profile_me_inactive_user_403(
    client: AsyncClient, inactive_student: User
) -> None:
    """Verify GET /api/v1/users/profile/me for deactivated account returns 403 Forbidden."""
    headers = make_auth_headers(inactive_student.id)
    response = await client.get("/api/v1/users/profile/me", headers=headers)
    assert response.status_code == 403

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_403"
    assert "inactive" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_api_put_profile_me_success_200(
    client: AsyncClient, active_student: User
) -> None:
    """Verify PUT /api/v1/users/profile/me updates student profile and returns 200."""
    headers = make_auth_headers(active_student.id)
    payload = {
        "full_name": "Alice Bob",
        "display_name": "Ally",
        "phone_number": "+84912345678",
        "university": "Hanoi University of Science and Technology",
        "major": "Information Technology",
        "academic_year": 3,
        "avatar_url": "https://storage.edusense.io/avatars/alice.png",
    }
    response = await client.put("/api/v1/users/profile/me", json=payload, headers=headers)
    assert response.status_code == 200

    body = response.json()
    assert body["success"] is True
    assert body["message"] == "User profile updated successfully."

    data = body["data"]
    assert data["full_name"] == "Alice Bob"
    assert data["display_name"] == "Ally"
    assert data["phone_number"] == "+84912345678"
    assert data["university"] == "Hanoi University of Science and Technology"
    assert data["major"] == "Information Technology"
    assert data["academic_year"] == 3
    assert data["avatar_url"] == "https://storage.edusense.io/avatars/alice.png"


@pytest.mark.asyncio
async def test_api_patch_profile_me_partial_update_200(
    client: AsyncClient, active_student: User
) -> None:
    """Verify PATCH /api/v1/users/profile/me updates only specified fields."""
    headers = make_auth_headers(active_student.id)

    # Initial update to set full profile
    await client.put(
        "/api/v1/users/profile/me",
        json={
            "full_name": "Alice Initial",
            "university": "Initial University",
            "academic_year": 1,
        },
        headers=headers,
    )

    # Partial update via PATCH: change only academic_year and major
    patch_payload = {
        "academic_year": 2,
        "major": "Data Science",
    }
    response = await client.patch("/api/v1/users/profile/me", json=patch_payload, headers=headers)
    assert response.status_code == 200

    data = response.json()["data"]
    assert data["academic_year"] == 2
    assert data["major"] == "Data Science"
    assert data["full_name"] == "Alice Initial"
    assert data["university"] == "Initial University"


@pytest.mark.asyncio
async def test_api_put_profile_me_invalid_academic_year_zero_422(
    client: AsyncClient, active_student: User
) -> None:
    """Verify PUT /api/v1/users/profile/me with academic_year=0 returns 422."""
    headers = make_auth_headers(active_student.id)
    payload = {"academic_year": 0}
    response = await client.put("/api/v1/users/profile/me", json=payload, headers=headers)
    assert response.status_code == 422

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_api_put_profile_me_invalid_academic_year_fifteen_422(
    client: AsyncClient, active_student: User
) -> None:
    """Verify PUT /api/v1/users/profile/me with academic_year=15 returns 422."""
    headers = make_auth_headers(active_student.id)
    payload = {"academic_year": 15}
    response = await client.put("/api/v1/users/profile/me", json=payload, headers=headers)
    assert response.status_code == 422

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_api_patch_profile_me_invalid_academic_year_422(
    client: AsyncClient, active_student: User
) -> None:
    """Verify PATCH /api/v1/users/profile/me with academic_year=11 returns 422."""
    headers = make_auth_headers(active_student.id)
    payload = {"academic_year": 11}
    response = await client.patch("/api/v1/users/profile/me", json=payload, headers=headers)
    assert response.status_code == 422

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_api_put_profile_me_whitespace_full_name_422(
    client: AsyncClient, active_student: User
) -> None:
    """Verify PUT /api/v1/users/profile/me with whitespace full_name returns 422."""
    headers = make_auth_headers(active_student.id)
    payload = {"full_name": "     "}
    response = await client.put("/api/v1/users/profile/me", json=payload, headers=headers)
    assert response.status_code == 422

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_api_patch_profile_me_whitespace_full_name_422(
    client: AsyncClient, active_student: User
) -> None:
    """Verify PATCH /api/v1/users/profile/me with whitespace full_name returns 422."""
    headers = make_auth_headers(active_student.id)
    payload = {"full_name": "   \t\n  "}
    response = await client.patch("/api/v1/users/profile/me", json=payload, headers=headers)
    assert response.status_code == 422

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_api_put_profile_me_unauthenticated_401(client: AsyncClient) -> None:
    """Verify PUT /api/v1/users/profile/me without Authorization header returns 401."""
    payload = {"full_name": "Anonymous Update"}
    response = await client.put("/api/v1/users/profile/me", json=payload)
    assert response.status_code == 401

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"


@pytest.mark.asyncio
async def test_api_put_profile_me_expired_token_401(
    client: AsyncClient, active_student: User
) -> None:
    """Verify PUT /api/v1/users/profile/me with expired token returns 401."""
    headers = make_auth_headers(active_student.id, expires_seconds=-60)
    payload = {"full_name": "Expired Attempt"}
    response = await client.put("/api/v1/users/profile/me", json=payload, headers=headers)
    assert response.status_code == 401

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"


@pytest.mark.asyncio
async def test_api_put_profile_me_invalid_token_401(client: AsyncClient) -> None:
    """Verify PUT /api/v1/users/profile/me with corrupt token returns 401."""
    headers = {"Authorization": "Bearer corrupt.token.signature"}
    payload = {"full_name": "Corrupt Attempt"}
    response = await client.put("/api/v1/users/profile/me", json=payload, headers=headers)
    assert response.status_code == 401

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_401"


@pytest.mark.asyncio
async def test_api_put_profile_me_inactive_user_403(
    client: AsyncClient, inactive_student: User
) -> None:
    """Verify PUT /api/v1/users/profile/me for inactive user returns 403 Forbidden."""
    headers = make_auth_headers(inactive_student.id)
    payload = {"full_name": "Inactive Attempt"}
    response = await client.put("/api/v1/users/profile/me", json=payload, headers=headers)
    assert response.status_code == 403

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_403"


@pytest.mark.asyncio
async def test_api_patch_profile_me_inactive_user_403(
    client: AsyncClient, inactive_student: User
) -> None:
    """Verify PATCH /api/v1/users/profile/me for inactive user returns 403 Forbidden."""
    headers = make_auth_headers(inactive_student.id)
    payload = {"academic_year": 2}
    response = await client.patch("/api/v1/users/profile/me", json=payload, headers=headers)
    assert response.status_code == 403

    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_403"


@pytest.mark.asyncio
async def test_api_academic_year_boundary_valid(
    client: AsyncClient, active_student: User
) -> None:
    """Verify academic_year bounds: 1 and 10 are accepted."""
    headers = make_auth_headers(active_student.id)

    # Lower bound: 1
    resp1 = await client.patch("/api/v1/users/profile/me", json={"academic_year": 1}, headers=headers)
    assert resp1.status_code == 200
    assert resp1.json()["data"]["academic_year"] == 1

    # Upper bound: 10
    resp10 = await client.patch("/api/v1/users/profile/me", json={"academic_year": 10}, headers=headers)
    assert resp10.status_code == 200
    assert resp10.json()["data"]["academic_year"] == 10

    # Null / reset
    resp_null = await client.patch("/api/v1/users/profile/me", json={"academic_year": None}, headers=headers)
    assert resp_null.status_code == 200
    assert resp_null.json()["data"]["academic_year"] is None


@pytest.mark.asyncio
async def test_api_alias_me_endpoint(
    client: AsyncClient, active_student: User
) -> None:
    """Verify /api/v1/users/me functions as alias to /api/v1/users/profile/me."""
    headers = make_auth_headers(active_student.id)
    resp = await client.get("/api/v1/users/me", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["user_id"] == str(active_student.id)
