"""Admin student accounts management API endpoints (US 2.22)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

from app.core.security import get_current_active_user
from app.services.admin_service import (
    AdminActionError,
    AdminUserNotFoundError,
    AdminUserService,
    StudentDetailDTO,
    get_admin_user_service,
)
from app.services.auth_service import UserDTO

router = APIRouter(prefix="/api/v1/admin/users", tags=["admin-users"])


# ---------------------------------------------------------------------------
# RBAC Guard
# ---------------------------------------------------------------------------


async def require_admin(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
) -> UserDTO:
    """Enforce strict RBAC: only active accounts with 'admin' role are permitted."""
    if current_user.role.lower() != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required.",
        )
    return current_user


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class StudentProfileData(BaseModel):
    """Student profile attributes."""

    id: uuid.UUID | None = None
    user_id: uuid.UUID
    full_name: str
    display_name: str | None = None
    avatar_url: str | None = None
    phone_number: str | None = None
    university: str | None = None
    major: str | None = None
    academic_year: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class StudentSubscriptionData(BaseModel):
    """Student active subscription information."""

    id: uuid.UUID | None = None
    plan_code: str
    plan_name: str
    status: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool
    days_remaining: int = 0


class StudentListItemData(BaseModel):
    """Student summary representation for paginated lists."""

    id: uuid.UUID
    email: str
    full_name: str
    display_name: str | None = None
    avatar_url: str | None = None
    is_active: bool
    is_email_verified: bool
    current_plan_code: str
    roles: list[str]
    university: str | None = None
    major: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None


class StudentDetailData(BaseModel):
    """Comprehensive student account details."""

    id: uuid.UUID
    email: str
    full_name: str
    display_name: str | None = None
    avatar_url: str | None = None
    is_active: bool
    is_email_verified: bool
    current_plan_code: str
    roles: list[str]
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None
    profile: StudentProfileData | None = None
    subscription: StudentSubscriptionData | None = None


class PaginationMeta(BaseModel):
    """Pagination metadata block."""

    page: int
    page_size: int
    total_count: int
    total_pages: int


class StudentListResponse(BaseModel):
    """Standard envelope for paginated student listing."""

    success: bool = True
    data: list[StudentListItemData]
    meta: PaginationMeta
    message: str = "Student accounts retrieved successfully."


class StudentDetailResponse(BaseModel):
    """Standard envelope for single student account detail."""

    success: bool = True
    data: StudentDetailData
    message: str = "Student account details retrieved successfully."


class UpdateStudentStatusRequest(BaseModel):
    """Payload to activate or suspend a student account."""

    is_active: bool
    reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Audit reason for status change (required)",
    )

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        """Ensure reason contains meaningful non-whitespace text."""
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "value_error", "Reason cannot be empty or whitespace."
            )
        return stripped


class OverrideStudentPlanRequest(BaseModel):
    """Payload to manually override a student's subscription plan."""

    plan_code: str = Field(
        ...,
        min_length=1,
        max_length=20,
        description="Target subscription plan code (e.g. 'free', 'pro')",
    )
    duration_days: int | None = Field(
        default=None,
        ge=1,
        le=3650,
        description="Duration in days for paid plans (defaults to plan default or 30)",
    )
    reason: str = Field(
        default="",
        max_length=500,
        description="Optional audit justification for overriding plan",
    )

    @field_validator("plan_code")
    @classmethod
    def validate_plan_code(cls, value: str) -> str:
        """Strip and validate plan_code."""
        stripped = value.strip().lower()
        if not stripped:
            raise PydanticCustomError(
                "value_error", "Plan code cannot be empty or whitespace."
            )
        return stripped


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _dto_to_detail_data(dto: StudentDetailDTO) -> StudentDetailData:
    """Helper converting StudentDetailDTO to Pydantic StudentDetailData."""
    profile_data: StudentProfileData | None = None
    if dto.profile:
        profile_data = StudentProfileData(
            id=dto.profile.id,
            user_id=dto.profile.user_id,
            full_name=dto.profile.full_name,
            display_name=dto.profile.display_name,
            avatar_url=dto.profile.avatar_url,
            phone_number=dto.profile.phone_number,
            university=dto.profile.university,
            major=dto.profile.major,
            academic_year=dto.profile.academic_year,
            created_at=dto.profile.created_at,
            updated_at=dto.profile.updated_at,
        )

    subscription_data: StudentSubscriptionData | None = None
    if dto.subscription:
        subscription_data = StudentSubscriptionData(
            id=dto.subscription.id,
            plan_code=dto.subscription.plan_code,
            plan_name=dto.subscription.plan_name,
            status=dto.subscription.status,
            starts_at=dto.subscription.starts_at,
            ends_at=dto.subscription.ends_at,
            is_active=dto.subscription.is_active,
            days_remaining=dto.subscription.days_remaining,
        )

    return StudentDetailData(
        id=dto.id,
        email=dto.email,
        full_name=dto.full_name,
        display_name=dto.display_name,
        avatar_url=dto.avatar_url,
        is_active=dto.is_active,
        is_email_verified=dto.is_email_verified,
        current_plan_code=dto.current_plan_code,
        roles=dto.roles,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
        last_login_at=dto.last_login_at,
        profile=profile_data,
        subscription=subscription_data,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=StudentListResponse,
    status_code=status.HTTP_200_OK,
    summary="List student accounts with search, filters, and pagination",
)
async def list_students(
    _: Annotated[UserDTO, Depends(require_admin)],
    admin_service: Annotated[AdminUserService, Depends(get_admin_user_service)],
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    search: str | None = Query(default=None, description="Search by email or name"),
    is_active: bool | None = Query(default=None, description="Filter by active status"),
    plan_code: str | None = Query(default=None, description="Filter by plan code"),
) -> StudentListResponse:
    """List student accounts with optional search by email/name, filtering, and pagination."""
    result = await admin_service.list_students(
        page=page,
        page_size=page_size,
        search=search,
        is_active=is_active,
        plan_code=plan_code,
    )

    items = [
        StudentListItemData(
            id=item.id,
            email=item.email,
            full_name=item.full_name,
            display_name=item.display_name,
            avatar_url=item.avatar_url,
            is_active=item.is_active,
            is_email_verified=item.is_email_verified,
            current_plan_code=item.current_plan_code,
            roles=item.roles,
            university=item.university,
            major=item.major,
            created_at=item.created_at,
            last_login_at=item.last_login_at,
        )
        for item in result.items
    ]

    return StudentListResponse(
        success=True,
        data=items,
        meta=PaginationMeta(
            page=result.page,
            page_size=result.page_size,
            total_count=result.total_count,
            total_pages=result.total_pages,
        ),
        message="Student accounts retrieved successfully.",
    )


@router.get(
    "/{user_id}",
    response_model=StudentDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve full student account details",
)
async def get_student_detail(
    user_id: uuid.UUID,
    _: Annotated[UserDTO, Depends(require_admin)],
    admin_service: Annotated[AdminUserService, Depends(get_admin_user_service)],
) -> StudentDetailResponse:
    """Retrieve complete profile, plan, and status details for a specific student."""
    try:
        dto = await admin_service.get_student_detail(student_id=user_id)
    except AdminUserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return StudentDetailResponse(
        success=True,
        data=_dto_to_detail_data(dto),
        message="Student account details retrieved successfully.",
    )


@router.patch(
    "/{user_id}/status",
    response_model=StudentDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Activate or suspend a student account",
)
async def update_student_status(
    user_id: uuid.UUID,
    payload: UpdateStudentStatusRequest,
    admin: Annotated[UserDTO, Depends(require_admin)],
    admin_service: Annotated[AdminUserService, Depends(get_admin_user_service)],
    request: Request,
) -> StudentDetailResponse:
    """Activate or suspend a student account.

    On suspension, active refresh sessions are revoked immediately and an audit event is logged.
    """
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    try:
        dto = await admin_service.update_student_status(
            admin_user=admin,
            student_id=user_id,
            is_active=payload.is_active,
            reason=payload.reason,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except AdminUserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except AdminActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    status_str = "activated" if payload.is_active else "suspended"
    return StudentDetailResponse(
        success=True,
        data=_dto_to_detail_data(dto),
        message=f"Student account has been {status_str} successfully.",
    )


@router.patch(
    "/{user_id}/plan",
    response_model=StudentDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Override student subscription plan manually",
)
async def override_student_plan(
    user_id: uuid.UUID,
    payload: OverrideStudentPlanRequest,
    admin: Annotated[UserDTO, Depends(require_admin)],
    admin_service: Annotated[AdminUserService, Depends(get_admin_user_service)],
    request: Request,
) -> StudentDetailResponse:
    """Manually assign or override a student's subscription plan with audit logging."""
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    try:
        dto = await admin_service.override_student_plan(
            admin_user=admin,
            student_id=user_id,
            plan_code=payload.plan_code,
            duration_days=payload.duration_days,
            reason=payload.reason,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except AdminUserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except AdminActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return StudentDetailResponse(
        success=True,
        data=_dto_to_detail_data(dto),
        message=f"Student subscription plan has been updated to '{payload.plan_code}'.",
    )
