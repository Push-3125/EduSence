"""Authentication service handling registration business logic, password validation, and user creation."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import (
    PasswordStrengthError,
    enforce_password_strength,
    hash_password,
)
from app.repositories.user_repo import UserRepository


class AuthServiceError(Exception):
    """Base exception for authentication service errors."""


class UserAlreadyExistsError(AuthServiceError):
    """Raised when an account with the provided email already exists."""


class WeakPasswordError(AuthServiceError):
    """Raised when the provided password does not satisfy security requirements."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


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


class AuthService:
    """Service layer encapsulating authentication and account lifecycle logic."""

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


def get_auth_service(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AuthService:
    """FastAPI dependency provider for AuthService with an active database session."""
    return AuthService(user_repo=UserRepository(session))
