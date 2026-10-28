"""Authentication API router: Registration, login, refresh, logout, password reset, and profile endpoints."""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

from app.core.config import settings
from app.core.security import InvalidTokenError, get_current_active_user
from app.services.auth_service import (
    AuthService,
    AuthServiceError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidSessionError,
    RateLimitExceededError,
    UserAlreadyExistsError,
    UserDTO,
    WeakPasswordError,
    get_auth_service,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


# ---------------------------------------------------------------------------
# Request and Response Schemas
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Payload schema for email and password student registration."""

    email: str = Field(..., max_length=255, description="Valid student email address")
    password: str = Field(..., min_length=1, max_length=128, description="Account password")
    full_name: str = Field(..., min_length=1, max_length=150, description="Student full name")

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, value: str) -> str:
        """Ensure email conforms to a valid standard email address pattern."""
        normalized = value.strip().lower()
        if not normalized or not EMAIL_REGEX.match(normalized):
            raise PydanticCustomError("value_error", "Invalid email address format.")
        return normalized

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str) -> str:
        """Ensure full name is not composed solely of whitespace."""
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "value_error", "Full name cannot be empty or whitespace."
            )
        return stripped

    @field_validator("password")
    @classmethod
    def validate_password_non_empty(cls, value: str) -> str:
        """Ensure password is provided."""
        if not value:
            raise PydanticCustomError("value_error", "Password cannot be empty.")
        return value


class LoginRequest(BaseModel):
    """Payload schema for student and admin authentication."""

    email: str = Field(..., max_length=255, description="Registered account email")
    password: str = Field(..., min_length=1, max_length=128, description="Account password")

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, value: str) -> str:
        """Normalize and validate email format."""
        normalized = value.strip().lower()
        if not normalized or not EMAIL_REGEX.match(normalized):
            raise PydanticCustomError("value_error", "Invalid email address format.")
        return normalized

    @field_validator("password")
    @classmethod
    def validate_password_non_empty(cls, value: str) -> str:
        """Ensure password is provided."""
        if not value:
            raise PydanticCustomError("value_error", "Password cannot be empty.")
        return value


class RefreshRequest(BaseModel):
    """Payload schema for renewing an access token using a refresh token."""

    refresh_token: str = Field(..., min_length=1, description="Active refresh token")


class LogoutRequest(BaseModel):
    """Payload schema for revoking a refresh token session."""

    refresh_token: str = Field(..., min_length=1, description="Refresh token to revoke")


class UserData(BaseModel):
    """Serialized public user information without credentials or hashes."""

    id: uuid.UUID
    email: str
    full_name: str
    role: str
    current_plan_code: str
    is_active: bool
    is_email_verified: bool
    created_at: datetime
    avatar_url: str | None = None


class GoogleLoginUrlData(BaseModel):
    """Payload containing Google OAuth authorization URL."""

    url: str


class GoogleLoginUrlResponse(BaseModel):
    """Standardized response envelope for Google OAuth URL."""

    success: bool = True
    data: GoogleLoginUrlData
    message: str = "Google OAuth authorization URL generated successfully."


class GoogleAuthRequest(BaseModel):
    """Payload schema for Google OAuth2 code exchange."""

    code: str = Field(..., min_length=1, description="Google OAuth authorization code")
    state: str | None = Field(default=None, description="Optional OAuth state parameter")


class RegisterResponse(BaseModel):
    """Standardized success response envelope for user registration."""

    success: bool = True
    data: UserData
    message: str = "User registered successfully."


class TokenResponseData(BaseModel):
    """Payload containing access token, refresh token, and authenticated user details."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserData


class TokenResponse(BaseModel):
    """Standardized success response envelope for user login."""

    success: bool = True
    data: TokenResponseData
    message: str = "Login successful."


class RefreshTokenData(BaseModel):
    """Payload containing a newly issued access token."""

    access_token: str
    token_type: str = "bearer"
    refresh_token: str | None = None


class RefreshTokenResponse(BaseModel):
    """Standardized response envelope for refreshed access token."""

    success: bool = True
    data: RefreshTokenData
    message: str = "Token refreshed successfully."


class LogoutResponse(BaseModel):
    """Standardized response envelope for session revocation."""

    success: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = "Logged out successfully."


class UserMeResponse(BaseModel):
    """Standardized response envelope for current authenticated user profile."""

    success: bool = True
    data: UserData
    message: str = "Current user profile retrieved successfully."


class PasswordResetRequest(BaseModel):
    """Payload schema for requesting a password reset token."""

    email: str = Field(..., max_length=255, description="Registered account email")

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, value: str) -> str:
        """Normalize and validate email format."""
        normalized = value.strip().lower()
        if not normalized or not EMAIL_REGEX.match(normalized):
            raise PydanticCustomError("value_error", "Invalid email address format.")
        return normalized


class PasswordResetConfirmRequest(BaseModel):
    """Payload schema for confirming password reset with verification token."""

    token: str = Field(..., min_length=1, description="Password reset verification token")
    new_password: str = Field(
        ..., min_length=1, max_length=128, description="New account password"
    )

    @field_validator("token")
    @classmethod
    def validate_token_non_empty(cls, value: str) -> str:
        """Ensure token is not empty or whitespace."""
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError("value_error", "Token cannot be empty.")
        return stripped

    @field_validator("new_password")
    @classmethod
    def validate_password_non_empty(cls, value: str) -> str:
        """Ensure new password is provided."""
        if not value:
            raise PydanticCustomError("value_error", "New password cannot be empty.")
        return value


class PasswordResetRequestResponse(BaseModel):
    """Standardized response envelope for password reset request."""

    success: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = (
        "If an account exists with this email, a password reset link has been sent."
    )
    dev_token: str | None = None


class PasswordResetConfirmResponse(BaseModel):
    """Standardized response envelope for password reset confirmation."""

    success: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = "Password has been reset successfully."


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterResponse,
    summary="Register a new student account",
    description="Create a new student account using email, password, and full name.",
)
async def register(
    payload: RegisterRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> RegisterResponse:
    """Register a new student account and create their default profile and role."""
    try:
        user_dto = await auth_service.register_user(
            email=payload.email,
            password=payload.password,
            full_name=payload.full_name,
        )
        return RegisterResponse(
            success=True,
            data=UserData(
                id=user_dto.id,
                email=user_dto.email,
                full_name=user_dto.full_name,
                role=user_dto.role,
                current_plan_code=user_dto.current_plan_code,
                is_active=user_dto.is_active,
                is_email_verified=user_dto.is_email_verified,
                created_at=user_dto.created_at,
                avatar_url=user_dto.avatar_url,
            ),
            message="User registered successfully.",
        )
    except UserAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except WeakPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


def get_client_ip(request: Request) -> str | None:
    """Extract client IP address from X-Forwarded-For header or direct client connection."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
        if client_ip:
            return client_ip
    if request.client and request.client.host:
        return request.client.host
    return None


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    response_model=TokenResponse,
    summary="User login and token issuance",
    description="Authenticate user with email and password, returning JWT access and refresh tokens.",
)
async def login(
    payload: LoginRequest,
    request: Request,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse | JSONResponse:
    """Authenticate credentials and issue JWT access and refresh token pair."""
    user_agent = request.headers.get("user-agent")
    ip_address = get_client_ip(request)

    try:
        token_dto = await auth_service.login_user(
            email=payload.email,
            password=payload.password,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        return TokenResponse(
            success=True,
            data=TokenResponseData(
                access_token=token_dto.access_token,
                refresh_token=token_dto.refresh_token,
                token_type=token_dto.token_type,
                user=UserData(
                    id=token_dto.user.id,
                    email=token_dto.user.email,
                    full_name=token_dto.user.full_name,
                    role=token_dto.user.role,
                    current_plan_code=token_dto.user.current_plan_code,
                    is_active=token_dto.user.is_active,
                    is_email_verified=token_dto.user.is_email_verified,
                    created_at=token_dto.user.created_at,
                    avatar_url=token_dto.user.avatar_url,
                ),
            ),
            message="Login successful.",
        )
    except RateLimitExceededError as exc:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(exc.retry_after_seconds)},
            content={
                "success": False,
                "error": {
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": "Too many failed login attempts. Please try again later.",
                    "retry_after": exc.retry_after_seconds,
                },
            },
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except InactiveUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post(
    "/refresh",
    status_code=status.HTTP_200_OK,
    response_model=RefreshTokenResponse,
    summary="Refresh access token",
    description="Renew an expired access token using an active refresh token.",
)
async def refresh_token(
    payload: RefreshRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> RefreshTokenResponse:
    """Validate refresh token and generate a new access token."""
    try:
        new_access_token = await auth_service.refresh_access_token(
            refresh_token=payload.refresh_token
        )
        return RefreshTokenResponse(
            success=True,
            data=RefreshTokenData(
                access_token=new_access_token,
                token_type="bearer",
            ),
            message="Token refreshed successfully.",
        )
    except InvalidSessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except InactiveUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


@router.post(
    "/logout",
    status_code=status.HTTP_200_OK,
    response_model=LogoutResponse,
    summary="Revoke refresh token session",
    description="Revoke the provided refresh token session, preventing subsequent token refresh.",
)
async def logout(
    payload: LogoutRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> LogoutResponse:
    """Revoke the current refresh token session."""
    await auth_service.logout_user(refresh_token=payload.refresh_token)
    return LogoutResponse(
        success=True,
        data={},
        message="Logged out successfully.",
    )


@router.get(
    "/me",
    status_code=status.HTTP_200_OK,
    response_model=UserMeResponse,
    summary="Get current user profile",
    description="Retrieve the authenticated user's profile and RBAC role.",
)
async def get_me(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
) -> UserMeResponse:
    """Fetch current authenticated user profile."""
    return UserMeResponse(
        success=True,
        data=UserData(
            id=current_user.id,
            email=current_user.email,
            full_name=current_user.full_name,
            role=current_user.role,
            current_plan_code=current_user.current_plan_code,
            is_active=current_user.is_active,
            is_email_verified=current_user.is_email_verified,
            created_at=current_user.created_at,
            avatar_url=current_user.avatar_url,
        ),
        message="Current user profile retrieved successfully.",
    )


@router.get(
    "/google/url",
    status_code=status.HTTP_200_OK,
    response_model=GoogleLoginUrlResponse,
    summary="Generate Google OAuth authorization URL",
    description="Generates Google OAuth 2.0 authorization URL for student social login.",
)
async def get_google_auth_url(
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    state: str | None = None,
) -> GoogleLoginUrlResponse:
    """Generate Google OAuth 2.0 authorization URL."""
    try:
        url = auth_service.generate_google_auth_url(state=state)
        return GoogleLoginUrlResponse(
            success=True,
            data=GoogleLoginUrlData(url=url),
            message="Google OAuth authorization URL generated successfully.",
        )
    except AuthServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post(
    "/google/callback",
    status_code=status.HTTP_200_OK,
    response_model=TokenResponse,
    summary="Google OAuth login callback",
    description="Exchange Google authorization code for JWT access and refresh tokens.",
)
async def google_callback(
    payload: GoogleAuthRequest,
    request: Request,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    """Authenticate student via Google OAuth code exchange."""
    user_agent = request.headers.get("user-agent")
    ip_address = request.client.host if request.client else None

    try:
        token_dto = await auth_service.authenticate_google_user(
            code=payload.code,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        return TokenResponse(
            success=True,
            data=TokenResponseData(
                access_token=token_dto.access_token,
                refresh_token=token_dto.refresh_token,
                token_type=token_dto.token_type,
                user=UserData(
                    id=token_dto.user.id,
                    email=token_dto.user.email,
                    full_name=token_dto.user.full_name,
                    role=token_dto.user.role,
                    current_plan_code=token_dto.user.current_plan_code,
                    is_active=token_dto.user.is_active,
                    is_email_verified=token_dto.user.is_email_verified,
                    created_at=token_dto.user.created_at,
                    avatar_url=token_dto.user.avatar_url,
                ),
            ),
            message="Login successful.",
        )
    except InactiveUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except AuthServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post(
    "/password-reset/request",
    status_code=status.HTTP_200_OK,
    response_model=PasswordResetRequestResponse,
    summary="Request password reset",
    description="Generate a single-use password reset verification token for the specified email.",
)
async def request_password_reset(
    payload: PasswordResetRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> PasswordResetRequestResponse:
    """Request a password reset link/token."""
    _user_found, raw_token = await auth_service.request_password_reset(
        email=payload.email
    )
    dev_token_val = raw_token if settings.debug else None
    data_dict = {"dev_token": dev_token_val} if dev_token_val else {}
    return PasswordResetRequestResponse(
        success=True,
        data=data_dict,
        dev_token=dev_token_val,
        message="If an account exists with this email, a password reset link has been sent.",
    )


@router.post(
    "/password-reset/confirm",
    status_code=status.HTTP_200_OK,
    response_model=PasswordResetConfirmResponse,
    summary="Confirm password reset",
    description="Reset account password using a single-use verification token.",
)
async def confirm_password_reset(
    payload: PasswordResetConfirmRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> PasswordResetConfirmResponse:
    """Reset account password using a valid single-use verification token."""
    try:
        await auth_service.reset_password(
            token=payload.token,
            new_password=payload.new_password,
        )
        return PasswordResetConfirmResponse(
            success=True,
            data={},
            message="Password has been reset successfully.",
        )
    except WeakPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
