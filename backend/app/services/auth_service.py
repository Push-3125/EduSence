"""Authentication service handling registration, login, refresh sessions, and credential lifecycle."""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import quote, urlencode

import httpx
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
    avatar_url: str | None = None


@dataclass
class AuthTokensDTO:
    """Pair of access and refresh tokens along with public user metadata."""

    access_token: str
    refresh_token: str
    token_type: str
    user: UserDTO


def generate_google_auth_url(state: str | None = None) -> str:
    """Generate official Google OAuth 2.0 authorization URL for student social login."""
    if not settings.google_client_id:
        raise AuthServiceError("Google OAuth client configuration is missing.")

    params = {
        "response_type": "code",
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
    }
    if state:
        params["state"] = state

    query = urlencode(params, quote_via=quote)
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


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
            avatar_url=user.profile.avatar_url if user.profile else None,
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
            avatar_url=user.profile.avatar_url if user.profile else None,
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
            avatar_url=user.profile.avatar_url if user.profile else None,
        )

    def generate_google_auth_url(self, state: str | None = None) -> str:
        """Generate official Google OAuth 2.0 authorization URL for student social login."""
        return generate_google_auth_url(state=state)

    async def _call_google_oauth_endpoints(
        self,
        client: httpx.AsyncClient,
        code: str,
    ) -> dict[str, Any]:
        """Exchange authorization code and fetch user profile from Google OAuth2 endpoints."""
        token_endpoint = "https://oauth2.googleapis.com/token"
        token_data = {
            "code": code.strip(),
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect_uri,
            "grant_type": "authorization_code",
        }

        try:
            token_resp = await client.post(token_endpoint, data=token_data)
        except httpx.HTTPError as exc:
            raise AuthServiceError(
                f"Failed to authenticate with Google: Token request failed ({exc})"
            ) from exc

        if token_resp.status_code != 200:
            err_msg = "Invalid authorization code or token exchange failed."
            try:
                err_json = token_resp.json()
                err_msg = (
                    err_json.get("error_description")
                    or err_json.get("error")
                    or err_msg
                )
            except (ValueError, AttributeError):
                err_msg = token_resp.text or err_msg
            raise AuthServiceError(f"Failed to authenticate with Google: {err_msg}")

        token_payload = token_resp.json()
        google_access_token = token_payload.get("access_token")
        if not google_access_token:
            raise AuthServiceError("Failed to authenticate with Google: Missing access token.")

        userinfo_endpoint = "https://www.googleapis.com/oauth2/v3/userinfo"
        try:
            userinfo_resp = await client.get(
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {google_access_token}"},
            )
        except httpx.HTTPError as exc:
            raise AuthServiceError(
                f"Failed to authenticate with Google: Userinfo request failed ({exc})"
            ) from exc

        if userinfo_resp.status_code != 200:
            raise AuthServiceError(
                "Failed to authenticate with Google: Failed to retrieve user info from Google."
            )

        return userinfo_resp.json()

    async def authenticate_google_user(
        self,
        code: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> AuthTokensDTO:
        """Authenticate student via Google OAuth2 authorization code.

        Exchanges code for tokens, retrieves Google profile, validates verified email,
        provisions or updates the student account, records login audit, and issues JWT pair.

        Args:
            code: Google OAuth 2.0 authorization code.
            user_agent: Optional HTTP User-Agent client header.
            ip_address: Optional client IP address.
            client: Optional injected httpx.AsyncClient for testing.

        Returns:
            AuthTokensDTO containing access and refresh tokens and UserDTO.

        Raises:
            AuthServiceError: If code exchange, userinfo, or email verification fails.
            InactiveUserError: If the existing account has been deactivated.
        """
        if not code or not code.strip():
            raise AuthServiceError("Failed to authenticate with Google: Authorization code is required.")

        if not settings.google_client_id or not settings.google_client_secret:
            raise AuthServiceError("Failed to authenticate with Google: Client credentials not configured.")

        if client is not None:
            user_info = await self._call_google_oauth_endpoints(client, code)
        else:
            async with httpx.AsyncClient(timeout=10.0) as default_client:
                user_info = await self._call_google_oauth_endpoints(default_client, code)

        raw_email = user_info.get("email")
        if not raw_email or not isinstance(raw_email, str) or not raw_email.strip():
            raise AuthServiceError("Failed to authenticate with Google: Email not provided by Google.")

        clean_email = raw_email.lower().strip()
        email_verified = user_info.get("email_verified")
        if email_verified is not True and str(email_verified).lower() != "true":
            raise AuthServiceError("Failed to authenticate with Google: Google email is not verified.")

        google_picture = user_info.get("picture")
        google_name = (
            user_info.get("name")
            or user_info.get("given_name")
            or clean_email.split("@")[0]
        )

        user = await self.user_repo.get_by_email(clean_email)
        if user is not None:
            if not user.is_active:
                raise InactiveUserError("User account is inactive. Please contact support.")

            if not user.is_email_verified:
                await self.user_repo.mark_email_verified(user.id)
                user.is_email_verified = True

            if google_picture and user.profile and not user.profile.avatar_url:
                await self.user_repo.update_profile_avatar(user.id, avatar_url=google_picture)
                user.profile.avatar_url = google_picture
        else:
            random_password = secrets.token_urlsafe(48)
            random_password_hash = hash_password(random_password)

            user = await self.user_repo.create_oauth_user(
                email=clean_email,
                password_hash=random_password_hash,
                full_name=google_name,
                avatar_url=google_picture,
                role_name="student",
                current_plan_code="free",
            )

        # Update last login timestamp
        await self.user_repo.update_last_login(user.id)

        role_name = user.roles[0].name if user.roles else "student"
        profile_name = user.profile.full_name if user.profile else clean_email
        avatar_url = user.profile.avatar_url if user.profile else None

        access_token = create_access_token(
            subject=str(user.id),
            extra_claims={"role": role_name, "email": user.email},
            expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
        )

        refresh_token = create_refresh_token(
            subject=str(user.id),
            expires_delta=timedelta(days=settings.refresh_token_expire_days),
        )

        token_h = hash_token(refresh_token)
        expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)

        await self.user_repo.create_refresh_session(
            user_id=user.id,
            token_hash=token_h,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )

        user_dto = UserDTO(
            id=user.id,
            email=user.email,
            full_name=profile_name,
            role=role_name,
            current_plan_code=user.current_plan_code,
            is_active=user.is_active,
            is_email_verified=user.is_email_verified,
            created_at=user.created_at,
            avatar_url=avatar_url,
        )

        return AuthTokensDTO(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user=user_dto,
        )


def get_auth_service(
    user_repo: Annotated[UserRepository, Depends(get_user_repo)],
) -> AuthService:
    """FastAPI dependency provider for AuthService with an active database repository."""
    return AuthService(user_repo=user_repo)
