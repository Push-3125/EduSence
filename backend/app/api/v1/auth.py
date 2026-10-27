"""Authentication API router: Registration, login, and credential lifecycle endpoints."""

import re
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

from app.services.auth_service import (
    AuthService,
    UserAlreadyExistsError,
    WeakPasswordError,
    get_auth_service,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


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


class RegisterResponse(BaseModel):
    """Standardized success response envelope for user registration."""

    success: bool = True
    data: UserData
    message: str = "User registered successfully."


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
