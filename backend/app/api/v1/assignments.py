"""Assignments CRUD API router for academic study management (US 2.6)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

from app.core.security import get_current_active_user
from app.services.assignment_service import (
    _UNSET,
    AssignmentDTO,
    AssignmentNotFoundError,
    AssignmentService,
    AssignmentValidationError,
    CourseNotFoundError,
    get_assignment_service,
)
from app.services.auth_service import UserDTO

router = APIRouter(prefix="/api/v1/assignments", tags=["assignments"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AssignmentCreateRequest(BaseModel):
    """Payload schema for creating a new assignment."""

    course_id: uuid.UUID = Field(..., description="Target course UUID")
    title: str = Field(
        ..., min_length=1, max_length=255, description="Assignment title or task name"
    )
    description: str | None = Field(
        default=None, description="Detailed instructions, syllabus, or notes"
    )
    due_at: datetime | None = Field(
        default=None, description="Due date and time with timezone"
    )
    priority: str = Field(
        default="medium",
        pattern="^(low|medium|high)$",
        description="Assignment priority ('low', 'medium', 'high')",
    )
    status: str = Field(
        default="pending",
        pattern="^(pending|in_progress|completed)$",
        description="Assignment status ('pending', 'in_progress', 'completed')",
    )

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Ensure title is not empty or composed solely of whitespace."""
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "value_error", "Assignment title cannot be empty or whitespace."
            )
        return stripped

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str | None) -> str | None:
        """Strip whitespace from optional description."""
        if value is not None:
            stripped = value.strip()
            return stripped or None
        return None


class AssignmentUpdateRequest(BaseModel):
    """Payload schema for updating existing assignment attributes."""

    course_id: uuid.UUID | None = Field(
        default=None, description="Reassigned course UUID"
    )
    title: str | None = Field(
        default=None, min_length=1, max_length=255, description="Assignment title"
    )
    description: str | None = Field(
        default=None, description="Detailed instructions, syllabus, or notes"
    )
    due_at: datetime | None = Field(
        default=None, description="Due date and time with timezone"
    )
    priority: str | None = Field(
        default=None,
        pattern="^(low|medium|high)$",
        description="Assignment priority ('low', 'medium', 'high')",
    )
    status: str | None = Field(
        default=None,
        pattern="^(pending|in_progress|completed)$",
        description="Assignment status ('pending', 'in_progress', 'completed')",
    )

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        """Ensure title is not empty or whitespace if provided."""
        if value is not None:
            stripped = value.strip()
            if not stripped:
                raise PydanticCustomError(
                    "value_error", "Assignment title cannot be empty or whitespace."
                )
            return stripped
        return None

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str | None) -> str | None:
        """Strip whitespace from optional description."""
        if value is not None:
            stripped = value.strip()
            return stripped or None
        return None


class AssignmentData(BaseModel):
    """Data payload representing assignment attributes."""

    id: uuid.UUID
    course_id: uuid.UUID
    title: str
    description: str | None = None
    due_at: datetime | None = None
    priority: str
    status: str
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AssignmentResponse(BaseModel):
    """Standardized API single assignment response envelope."""

    success: bool = True
    data: AssignmentData
    message: str = "Assignment operation successful."


class AssignmentListData(BaseModel):
    """Data payload for paginated assignment listing."""

    items: list[AssignmentData]
    total: int
    skip: int
    limit: int


class AssignmentListResponse(BaseModel):
    """Standardized API list response envelope for assignments."""

    success: bool = True
    data: AssignmentListData
    message: str = "Assignments retrieved successfully."


class AssignmentActionResponse(BaseModel):
    """Standardized API response envelope for mutation actions without data payload."""

    success: bool = True
    message: str


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _to_assignment_data(dto: AssignmentDTO) -> AssignmentData:
    """Map domain AssignmentDTO to presentation AssignmentData schema."""
    return AssignmentData(
        id=dto.id,
        course_id=dto.course_id,
        title=dto.title,
        description=dto.description,
        due_at=dto.due_at,
        priority=dto.priority,
        status=dto.status,
        completed_at=dto.completed_at,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=AssignmentResponse, status_code=status.HTTP_201_CREATED)
async def create_assignment(
    payload: AssignmentCreateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    assignment_service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> AssignmentResponse:
    """Create a new assignment under a course owned by the authenticated student."""
    try:
        dto = await assignment_service.create_assignment(
            user_id=current_user.id,
            course_id=payload.course_id,
            title=payload.title,
            description=payload.description,
            due_at=payload.due_at,
            priority=payload.priority,
            status=payload.status,
        )
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc
    except AssignmentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return AssignmentResponse(
        success=True,
        data=_to_assignment_data(dto),
        message="Assignment created successfully.",
    )


@router.get("", response_model=AssignmentListResponse, status_code=status.HTTP_200_OK)
async def list_assignments(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    assignment_service: Annotated[AssignmentService, Depends(get_assignment_service)],
    course_id: Annotated[
        uuid.UUID | None,
        Query(description="Filter assignments by course UUID"),
    ] = None,
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            pattern="^(pending|in_progress|completed)$",
            description="Filter by assignment status",
        ),
    ] = None,
    priority: Annotated[
        str | None,
        Query(
            pattern="^(low|medium|high)$",
            description="Filter by priority level",
        ),
    ] = None,
    skip: Annotated[
        int,
        Query(ge=0, description="Offset count for pagination"),
    ] = 0,
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Maximum items per page"),
    ] = 50,
) -> AssignmentListResponse:
    """List paginated assignments for the authenticated student with optional filters."""
    try:
        dtos, total = await assignment_service.list_assignments(
            user_id=current_user.id,
            course_id=course_id,
            status=status_filter,
            priority=priority,
            skip=skip,
            limit=limit,
        )
    except AssignmentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return AssignmentListResponse(
        success=True,
        data=AssignmentListData(
            items=[_to_assignment_data(dto) for dto in dtos],
            total=total,
            skip=skip,
            limit=limit,
        ),
        message="Assignments retrieved successfully.",
    )


@router.get("/{assignment_id}", response_model=AssignmentResponse, status_code=status.HTTP_200_OK)
async def get_assignment(
    assignment_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    assignment_service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> AssignmentResponse:
    """Retrieve details of a single assignment owned by the student."""
    try:
        dto = await assignment_service.get_assignment(
            assignment_id=assignment_id,
            user_id=current_user.id,
        )
    except AssignmentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assignment not found.",
        ) from exc

    return AssignmentResponse(
        success=True,
        data=_to_assignment_data(dto),
        message="Assignment retrieved successfully.",
    )


@router.put("/{assignment_id}", response_model=AssignmentResponse, status_code=status.HTTP_200_OK)
async def update_assignment(
    assignment_id: uuid.UUID,
    payload: AssignmentUpdateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    assignment_service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> AssignmentResponse:
    """Update an assignment owned by the authenticated student."""
    try:
        dto = await assignment_service.update_assignment(
            assignment_id=assignment_id,
            user_id=current_user.id,
            course_id=payload.course_id if "course_id" in payload.model_fields_set else _UNSET,
            title=payload.title if "title" in payload.model_fields_set else _UNSET,
            description=payload.description if "description" in payload.model_fields_set else _UNSET,
            due_at=payload.due_at if "due_at" in payload.model_fields_set else _UNSET,
            priority=payload.priority if "priority" in payload.model_fields_set else _UNSET,
            status=payload.status if "status" in payload.model_fields_set else _UNSET,
        )
    except AssignmentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assignment not found.",
        ) from exc
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc
    except AssignmentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return AssignmentResponse(
        success=True,
        data=_to_assignment_data(dto),
        message="Assignment updated successfully.",
    )


@router.patch("/{assignment_id}", response_model=AssignmentResponse, status_code=status.HTTP_200_OK)
async def patch_assignment(
    assignment_id: uuid.UUID,
    payload: AssignmentUpdateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    assignment_service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> AssignmentResponse:
    """Partially update an assignment owned by the authenticated student."""
    return await update_assignment(
        assignment_id=assignment_id,
        payload=payload,
        current_user=current_user,
        assignment_service=assignment_service,
    )


@router.delete("/{assignment_id}", response_model=AssignmentActionResponse, status_code=status.HTTP_200_OK)
async def delete_assignment(
    assignment_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    assignment_service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> AssignmentActionResponse:
    """Permanently delete an assignment owned by the authenticated student."""
    try:
        await assignment_service.delete_assignment(
            assignment_id=assignment_id,
            user_id=current_user.id,
        )
    except AssignmentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assignment not found.",
        ) from exc

    return AssignmentActionResponse(
        success=True,
        message="Assignment deleted successfully.",
    )
