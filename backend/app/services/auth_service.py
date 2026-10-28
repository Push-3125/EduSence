"""Authentication service handling registration, login, refresh sessions, and credential lifecycle."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends

from app.core.config import settings
from app.core.security import (
    TOKEN_TYPE_REFRESH,
    InvalidTokenError,
    PasswordStrengthError,
    TokenExpiredError,
    create_access_token,
    create_refresh_token,
    enforce_password_strength,
    hash_password,
    hash_token,
    verify_jwt_token,
    verify_password,
)
from app.repositories.user_repo import UserRepository, get_user_repo

# Constant dummy hash for timing-safe password verification when user is not found
_DUMMY_PASSWORD_HASH = hash_password("EduSense_Timing_Safe_Dummy_Password_123!")


class AuthServiceError(Exception):
    """Base exception for authentication service errors."""


class UserAlreadyExistsError(AuthServiceError):
    """Raised when an account with the provided email already exists."""


class WeakPasswordError(AuthServiceError):
    """Raised when the provided password does not satisfy security requirements."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class InvalidCredentialsError(AuthServiceError):
    """Raised when authentication credentials (email or password) are invalid."""


class InactiveUserError(AuthServiceError):
    """Raised when an authenticated account has been deactivated."""


class InvalidSessionError(AuthServiceError):
    """Raised when a refresh token session is invalid, expired, or revoked."""


@dataclass
class UserDTO:
    """Decoupled Data Transfer Object representing an authenticated or registered user."""

    id: uuid.UUID
    email: str
    full_name: str
    role: str
    current_plan_code: str
    is_active: bool
    is_email_verified: bool
    created_at: datetime


@dataclass
class AuthTokensDTO:
    """Pair of access and refresh tokens along with public user metadata."""

    access_token: str
    refresh_token: str
    token_type: str
    user: UserDTO


class AuthService:
    """Service layer encapsulating authentication, session, and account lifecycle logic."""

    def __init__(self, user_repo: UserRepository) -> None:
        self.user_repo = user_repo

    async def register_user(
        self,
        email: str,
        password: str,
        full_name: str,
    ) -> UserDTO:
        """Register a new student user with personal profile and secure hashed credentials.

        Args:
            email: Student email address.
            password: Plain text password candidate.
            full_name: Student full name.

        Returns:
            UserDTO containing registered user details.

        Raises:
            ValueError: If required fields are empty.
            WeakPasswordError: If password does not meet password policy requirements.
            UserAlreadyExistsError: If an account with the normalized email already exists.
        """
        clean_email = email.lower().strip()
        clean_name = full_name.strip()

        if not clean_email:
            raise ValueError("Email cannot be empty.")
        if not clean_name:
            raise ValueError("Full name cannot be empty.")

        # 1. Enforce password strength policy
        try:
            enforce_password_strength(password)
        except PasswordStrengthError as exc:
            raise WeakPasswordError(str(exc), errors=exc.errors) from exc

        # 2. Check if user with normalized email already exists
        existing_user = await self.user_repo.get_by_email(clean_email)
        if existing_user is not None:
            raise UserAlreadyExistsError(
                f"An account with email '{clean_email}' already exists."
            )

        # 3. Hash password using Argon2id
        hashed_password = hash_password(password)

        # 4. Create user, user profile, and assign default 'student' role
        user = await self.user_repo.create_user(
            email=clean_email,
            password_hash=hashed_password,
            full_name=clean_name,
            role_name="student",
            current_plan_code="free",
        )

        role_name = user.roles[0].name if user.roles else "student"
        profile_name = user.profile.full_name if user.profile else clean_name

        return UserDTO(
            id=user.id,
            email=user.email,
            full_name=profile_name,
            role=role_name,
            current_plan_code=user.current_plan_code,
            is_active=user.is_active,
            is_email_verified=user.is_email_verified,
            created_at=user.created_at,
        )

    async def login_user(
        self,
        email: str,
        password: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AuthTokensDTO:
        """Authenticate user credentials and issue signed access & refresh token pair.

        Enforces timing-safe credential verification to protect against account enumeration.

        Args:
            email: Candidate user email.
            password: Plain text password.
            user_agent: Optional HTTP User-Agent client identifier.
            ip_address: Optional client IP address for session audit.

        Returns:
            AuthTokensDTO containing JWT access token, refresh token, and UserDTO.

        Raises:
            ValueError: If email or password is empty.
            InvalidCredentialsError: If credentials do not match any active record.
            InactiveUserError: If user account has been disabled.
        """
        clean_email = email.lower().strip()
        if not clean_email or not password:
            raise ValueError("Email and password cannot be empty.")

        user = await self.user_repo.get_by_email(clean_email)

        if user is None:
            # Perform dummy Argon2 verification to maintain constant computation time
            verify_password(password, _DUMMY_PASSWORD_HASH)
            raise InvalidCredentialsError("Invalid email or password.")

        if not verify_password(password, user.password_hash):
            raise InvalidCredentialsError("Invalid email or password.")

        if not user.is_active:
            raise InactiveUserError("User account is inactive. Please contact support.")

        role_name = user.roles[0].name if user.roles else "student"
        profile_name = user.profile.full_name if user.profile else user.email

        # Issue Access Token (15m default) and Refresh Token (7d default)
        access_token = create_access_token(
            subject=str(user.id),
            extra_claims={"role": role_name, "email": user.email},
            expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
        )

        refresh_token = create_refresh_token(
            subject=str(user.id),
            expires_delta=timedelta(days=settings.refresh_token_expire_days),
        )

        # Hash refresh token and persist session metadata
        token_h = hash_token(refresh_token)
        expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)

        await self.user_repo.create_refresh_session(
            user_id=user.id,
            token_hash=token_h,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )

        # Record last login timestamp
        await self.user_repo.update_last_login(user.id)

        user_dto = UserDTO(
            id=user.id,
            email=user.email,
            full_name=profile_name,
            role=role_name,
            current_plan_code=user.current_plan_code,
            is_active=user.is_active,
            is_email_verified=user.is_email_verified,
            created_at=user.created_at,
        )

        return AuthTokensDTO(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user=user_dto,
        )

    async def refresh_access_token(self, refresh_token: str) -> str:
        """Validate an active refresh token session and issue a refreshed access token.

        Args:
            refresh_token: Plain JWT refresh token string.

        Returns:
            New signed JWT access token string.

        Raises:
            InvalidSessionError: If the token is invalid, expired, revoked, or session missing.
            InactiveUserError: If the associated user account is inactive.
        """
        if not refresh_token:
            raise InvalidSessionError("Refresh token cannot be empty.")

        try:
            verify_jwt_token(refresh_token, expected_type=TOKEN_TYPE_REFRESH)
        except TokenExpiredError as exc:
            raise InvalidSessionError("Refresh token has expired.") from exc
        except InvalidTokenError as exc:
            raise InvalidSessionError(f"Invalid refresh token: {exc}") from exc

        token_h = hash_token(refresh_token)
        session = await self.user_repo.get_refresh_session_by_hash(token_h)

        if not session:
            raise InvalidSessionError("Refresh session not found or unrecognized.")

        if session.revoked_at is not None:
            raise InvalidSessionError("Refresh session has been revoked.")

        now = datetime.now(UTC)
        session_expires = session.expires_at
        if session_expires.tzinfo is None:
            session_expires = session_expires.replace(tzinfo=UTC)

        if session_expires <= now:
            raise InvalidSessionError("Refresh session has expired.")

        user = await self.user_repo.get_by_id(session.user_id)
        if not user:
            raise InvalidSessionError("User associated with session not found.")

        if not user.is_active:
            raise InactiveUserError("User account is inactive. Please contact support.")

        # Update last used timestamp on the refresh session
        await self.user_repo.update_session_last_used(session.id, last_used_at=now)

        role_name = user.roles[0].name if user.roles else "student"
        return create_access_token(
            subject=str(user.id),
            extra_claims={"role": role_name, "email": user.email},
            expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
        )

    async def logout_user(self, refresh_token: str) -> bool:
        """Revoke a refresh session, preventing subsequent token refresh.

        Idempotent operation: does not raise if the session is already revoked or missing.

        Args:
            refresh_token: Plain JWT refresh token string.

        Returns:
            True if an active session was found and revoked, False otherwise.
        """
        if not refresh_token:
            return False
        token_h = hash_token(refresh_token)
        return await self.user_repo.revoke_refresh_session(token_h)

    async def get_user_by_id(self, user_id: uuid.UUID) -> UserDTO | None:
        """Retrieve a user by primary UUID and map to UserDTO."""
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            return None

        role_name = user.roles[0].name if user.roles else "student"
        profile_name = user.profile.full_name if user.profile else user.email

        return UserDTO(
            id=user.id,
            email=user.email,
            full_name=profile_name,
            role=role_name,
            current_plan_code=user.current_plan_code,
            is_active=user.is_active,
            is_email_verified=user.is_email_verified,
            created_at=user.created_at,
        )


def get_auth_service(
    user_repo: Annotated[UserRepository, Depends(get_user_repo)],
) -> AuthService:
    """FastAPI dependency provider for AuthService with an active database repository."""
    return AuthService(user_repo=user_repo)
