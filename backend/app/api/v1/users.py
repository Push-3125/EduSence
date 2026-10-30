"""User profile and student information API router (US 2.4)."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

from app.core.security import get_current_active_user
from app.services.auth_service import UserDTO
from app.services.user_service import (
    NotFoundError,
    ProfileValidationError,
    UserService,
    UserServiceError,
    get_user_service,
)

router = APIRouter(prefix="/api/v1/users", tags=["users"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ProfileData(BaseModel):
    """Payload schema containing student profile details."""

    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    full_name: str
    display_name: str | None = None
    avatar_url: str | None = None
    phone_number: str | None = None
    university: str | None = None
    major: str | None = None
    academic_year: int | None = None
    created_at: datetime
    updated_at: datetime


class ProfileResponse(BaseModel):
    """Standardized API success response envelope for profile data."""

    success: bool = True
    data: ProfileData
    message: str = "User profile retrieved successfully."


class UpdateProfileRequest(BaseModel):
    """Payload schema for updating user profile and student information."""

    full_name: str | None = Field(
        default=None, max_length=150, description="Student full name (1-150 characters)"
    )
    display_name: str | None = Field(
        default=None, max_length=100, description="Student display name (up to 100 characters)"
    )
    avatar_url: str | None = Field(
        default=None, description="Public avatar image URL"
    )
    phone_number: str | None = Field(
        default=None, max_length=20, description="Contact phone number"
    )
    university: str | None = Field(
        default=None, max_length=255, description="University name"
    )
    major: str | None = Field(
        default=None, max_length=255, description="Major of study"
    )
    academic_year: int | None = Field(
        default=None, description="Academic year (between 1 and 10, or null)"
    )

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str | None) -> str | None:
        """Ensure full name is not composed solely of whitespace."""
        if value is not None:
            stripped = value.strip()
            if not stripped:
                raise PydanticCustomError(
                    "value_error", "Full name cannot be empty or whitespace."
                )
            return stripped
        return value

    @field_validator("academic_year")
    @classmethod
    def validate_academic_year(cls, value: int | None) -> int | None:
        """Validate academic year is within allowable university range 1-10."""
        if value is not None and (value < 1 or value > 10):
            raise PydanticCustomError(
                "value_error", "Academic year must be between 1 and 10."
            )
        return value

    @field_validator("display_name", "phone_number", "university", "major")
    @classmethod
    def strip_optional_strings(cls, value: str | None) -> str | None:
        """Strip leading/trailing whitespace from optional text attributes."""
        if value is not None:
            stripped = value.strip()
            return stripped or None
        return None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _process_profile_update(
    payload: UpdateProfileRequest,
    user_id: uuid.UUID,
    user_service: UserService,
    success_message: str,
) -> ProfileResponse:
    """Helper to apply profile update payload and construct response envelope."""
    update_data = payload.model_dump(exclude_unset=True)
    try:
        dto = await user_service.update_profile(user_id, **update_data)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ProfileValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except UserServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return ProfileResponse(
        success=True,
        data=ProfileData(
            id=dto.id,
            user_id=dto.user_id,
            email=dto.email,
            full_name=dto.full_name,
            display_name=dto.display_name,
            avatar_url=dto.avatar_url,
            phone_number=dto.phone_number,
            university=dto.university,
            major=dto.major,
            academic_year=dto.academic_year,
            created_at=dto.created_at or datetime.now(UTC),
            updated_at=dto.updated_at or datetime.now(UTC),
        ),
        message=success_message,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/profile/me",
    status_code=status.HTTP_200_OK,
    response_model=ProfileResponse,
    summary="Get current user profile",
    description="Retrieve the authenticated student's full profile and university information.",
)
@router.get(
    "/me",
    status_code=status.HTTP_200_OK,
    response_model=ProfileResponse,
    include_in_schema=False,
)
async def get_my_profile(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    user_service: Annotated[UserService, Depends(get_user_service)],
) -> ProfileResponse:
    """Fetch profile data for the authenticated student."""
    try:
        dto = await user_service.get_profile(current_user.id)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except UserServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return ProfileResponse(
        success=True,
        data=ProfileData(
            id=dto.id,
            user_id=dto.user_id,
            email=dto.email,
            full_name=dto.full_name,
            display_name=dto.display_name,
            avatar_url=dto.avatar_url,
            phone_number=dto.phone_number,
            university=dto.university,
            major=dto.major,
            academic_year=dto.academic_year,
            created_at=dto.created_at or datetime.now(UTC),
            updated_at=dto.updated_at or datetime.now(UTC),
        ),
        message="User profile retrieved successfully.",
    )


@router.put(
    "/profile/me",
    status_code=status.HTTP_200_OK,
    response_model=ProfileResponse,
    summary="Update current user profile",
    description="Full update of authenticated student's profile information.",
)
@router.put(
    "/me",
    status_code=status.HTTP_200_OK,
    response_model=ProfileResponse,
    include_in_schema=False,
)
async def update_my_profile_put(
    payload: UpdateProfileRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    user_service: Annotated[UserService, Depends(get_user_service)],
) -> ProfileResponse:
    """Full update of current user profile."""
    return await _process_profile_update(
        payload=payload,
        user_id=current_user.id,
        user_service=user_service,
        success_message="User profile updated successfully.",
    )


@router.patch(
    "/profile/me",
    status_code=status.HTTP_200_OK,
    response_model=ProfileResponse,
    summary="Partial update current user profile",
    description="Partial update of authenticated student's profile information.",
)
@router.patch(
    "/me",
    status_code=status.HTTP_200_OK,
    response_model=ProfileResponse,
    include_in_schema=False,
)
async def update_my_profile_patch(
    payload: UpdateProfileRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    user_service: Annotated[UserService, Depends(get_user_service)],
) -> ProfileResponse:
    """Partial update of current user profile."""
    return await _process_profile_update(
        payload=payload,
        user_id=current_user.id,
        user_service=user_service,
        success_message="User profile updated successfully.",
    )
