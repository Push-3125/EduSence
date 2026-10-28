"""User service handling student profile retrieval and profile update operations."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends

from app.repositories.user_repo import UserRepository, get_user_repo

_UNSET: Any = object()


class UserServiceError(Exception):
    """Base exception for user service errors."""


class NotFoundError(UserServiceError):
    """Raised when a user or user profile record is not found."""


class ProfileValidationError(UserServiceError):
    """Raised when profile update payload fails domain validation rules."""


@dataclass
class ProfileDTO:
    """Decoupled Data Transfer Object representing a student user profile."""

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
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UserService:
    """Service encapsulating user profile operations and business rules."""

    def __init__(self, user_repo: UserRepository) -> None:
        self.user_repo = user_repo

    async def get_profile(self, user_id: uuid.UUID) -> ProfileDTO:
        """Retrieve profile for the specified user UUID.

        Args:
            user_id: Target user UUID.

        Returns:
            ProfileDTO representing student profile.

        Raises:
            NotFoundError: If the user profile does not exist.
        """
        profile = await self.user_repo.get_profile_by_user_id(user_id)
        if not profile:
            raise NotFoundError(f"Profile for user {user_id} not found.")

        email = profile.user.email if profile.user else ""

        return ProfileDTO(
            id=profile.id,
            user_id=profile.user_id,
            email=email,
            full_name=profile.full_name,
            display_name=profile.display_name,
            avatar_url=profile.avatar_url,
            phone_number=profile.phone_number,
            university=profile.university,
            major=profile.major,
            academic_year=profile.academic_year,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )

    async def update_profile(
        self,
        user_id: uuid.UUID,
        full_name: str | None | Any = _UNSET,
        display_name: str | None | Any = _UNSET,
        phone_number: str | None | Any = _UNSET,
        university: str | None | Any = _UNSET,
        major: str | None | Any = _UNSET,
        academic_year: int | None | Any = _UNSET,
        avatar_url: str | None | Any = _UNSET,
        **kwargs: Any,
    ) -> ProfileDTO:
        """Update student profile with validated fields and commit changes.

        Args:
            user_id: User UUID whose profile is to be updated.
            full_name: Student full name (1-150 characters, non-whitespace).
            display_name: Optional display name (up to 100 characters).
            phone_number: Contact phone number (up to 20 characters).
            university: University name (up to 255 characters).
            major: Major / field of study (up to 255 characters).
            academic_year: Academic year (1-10 or None).
            avatar_url: Avatar image URL.
            **kwargs: Extra fields for compatibility.

        Returns:
            Updated ProfileDTO.

        Raises:
            NotFoundError: If the profile is not found.
            ProfileValidationError: If any provided field fails business rules.
        """
        update_fields: dict[str, Any] = {}

        if full_name is not _UNSET:
            if full_name is None:
                raise ProfileValidationError("Full name cannot be null or empty.")
            stripped_full_name = full_name.strip()
            if not stripped_full_name:
                raise ProfileValidationError("Full name cannot be empty or whitespace.")
            if len(stripped_full_name) > 150:
                raise ProfileValidationError("Full name cannot exceed 150 characters.")
            update_fields["full_name"] = stripped_full_name

        if display_name is not _UNSET:
            if display_name is not None:
                stripped_display = display_name.strip()
                if len(stripped_display) > 100:
                    raise ProfileValidationError("Display name cannot exceed 100 characters.")
                update_fields["display_name"] = stripped_display or None
            else:
                update_fields["display_name"] = None

        if phone_number is not _UNSET:
            if phone_number is not None:
                stripped_phone = phone_number.strip()
                if len(stripped_phone) > 20:
                    raise ProfileValidationError("Phone number cannot exceed 20 characters.")
                update_fields["phone_number"] = stripped_phone or None
            else:
                update_fields["phone_number"] = None

        if university is not _UNSET:
            if university is not None:
                stripped_uni = university.strip()
                if len(stripped_uni) > 255:
                    raise ProfileValidationError("University cannot exceed 255 characters.")
                update_fields["university"] = stripped_uni or None
            else:
                update_fields["university"] = None

        if major is not _UNSET:
            if major is not None:
                stripped_major = major.strip()
                if len(stripped_major) > 255:
                    raise ProfileValidationError("Major cannot exceed 255 characters.")
                update_fields["major"] = stripped_major or None
            else:
                update_fields["major"] = None

        if academic_year is not _UNSET:
            if academic_year is not None:
                if not isinstance(academic_year, int) or academic_year < 1 or academic_year > 10:
                    raise ProfileValidationError("Academic year must be an integer between 1 and 10.")
                update_fields["academic_year"] = academic_year
            else:
                update_fields["academic_year"] = None

        if avatar_url is not _UNSET:
            if avatar_url is not None:
                update_fields["avatar_url"] = avatar_url.strip() or None
            else:
                update_fields["avatar_url"] = None

        for k, v in kwargs.items():
            if k == "academic_year" and v is not _UNSET:
                if v is not None and (not isinstance(v, int) or v < 1 or v > 10):
                    raise ProfileValidationError("Academic year must be an integer between 1 and 10.")
                update_fields["academic_year"] = v
            elif k == "full_name" and v is not _UNSET:
                if v is None or not str(v).strip():
                    raise ProfileValidationError("Full name cannot be empty or whitespace.")
                update_fields["full_name"] = str(v).strip()
            elif k in {"display_name", "phone_number", "university", "major", "avatar_url"} and v is not _UNSET:
                update_fields[k] = v

        if not update_fields:
            return await self.get_profile(user_id)

        profile = await self.user_repo.update_profile(user_id, **update_fields)
        if not profile:
            raise NotFoundError(f"Profile for user {user_id} not found.")

        email = profile.user.email if profile.user else ""

        return ProfileDTO(
            id=profile.id,
            user_id=profile.user_id,
            email=email,
            full_name=profile.full_name,
            display_name=profile.display_name,
            avatar_url=profile.avatar_url,
            phone_number=profile.phone_number,
            university=profile.university,
            major=profile.major,
            academic_year=profile.academic_year,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )


def get_user_service(
    user_repo: Annotated[UserRepository, Depends(get_user_repo)],
) -> UserService:
    """FastAPI dependency provider for UserService."""
    return UserService(user_repo)


__all__ = [
    "NotFoundError",
    "ProfileDTO",
    "ProfileValidationError",
    "UserService",
    "UserServiceError",
    "get_user_service",
]
