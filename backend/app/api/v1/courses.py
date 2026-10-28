"""Courses CRUD API router for academic study management (US 2.5)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

from app.core.security import get_current_active_user
from app.services.auth_service import UserDTO
from app.services.course_service import (
    CourseConflictError,
    CourseDTO,
    CourseNotFoundError,
    CourseService,
    CourseServiceError,
    CourseValidationError,
    get_course_service,
)

router = APIRouter(prefix="/api/v1/courses", tags=["courses"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CourseCreateRequest(BaseModel):
    """Payload schema for creating a new university course."""

    name: str = Field(
        ..., min_length=1, max_length=255, description="Course title or subject name"
    )
    course_code: str | None = Field(
        default=None, max_length=50, description="Course code (e.g. CS101, MATH201)"
    )
    description: str | None = Field(
        default=None, description="Course syllabus, description, or notes"
    )
    instructor: str | None = Field(
        default=None, max_length=255, description="Instructor or professor name"
    )
    semester: str | None = Field(
        default=None, max_length=50, description="Semester or term (e.g. Fall 2026, Semester 1)"
    )
    academic_year: str | None = Field(
        default=None, max_length=20, description="Academic year (e.g. 2026-2027)"
    )
    status: str = Field(
        default="active",
        pattern="^(active|archived)$",
        description="Course status ('active' or 'archived')",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Ensure course name is not empty or composed solely of whitespace."""
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError("value_error", "Course name cannot be empty or whitespace.")
        return stripped

    @field_validator("course_code", "instructor", "semester", "academic_year", "description")
    @classmethod
    def strip_optional_strings(cls, value: str | None) -> str | None:
        """Strip surrounding whitespace from optional text attributes."""
        if value is not None:
            stripped = value.strip()
            return stripped or None
        return None


class CourseUpdateRequest(BaseModel):
    """Payload schema for updating existing course attributes."""

    name: str | None = Field(
        default=None, min_length=1, max_length=255, description="Course title or subject name"
    )
    course_code: str | None = Field(
        default=None, max_length=50, description="Course code (e.g. CS101)"
    )
    description: str | None = Field(
        default=None, description="Course syllabus, description, or notes"
    )
    instructor: str | None = Field(
        default=None, max_length=255, description="Instructor or professor name"
    )
    semester: str | None = Field(
        default=None, max_length=50, description="Semester or term"
    )
    academic_year: str | None = Field(
        default=None, max_length=20, description="Academic year"
    )
    status: str | None = Field(
        default=None,
        pattern="^(active|archived)$",
        description="Course status ('active' or 'archived')",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        """Ensure course name is not empty or whitespace if provided."""
        if value is not None:
            stripped = value.strip()
            if not stripped:
                raise PydanticCustomError("value_error", "Course name cannot be empty or whitespace.")
            return stripped
        return None

    @field_validator("course_code", "instructor", "semester", "academic_year", "description")
    @classmethod
    def strip_optional_strings(cls, value: str | None) -> str | None:
        """Strip surrounding whitespace from optional text attributes."""
        if value is not None:
            stripped = value.strip()
            return stripped or None
        return None


class CourseData(BaseModel):
    """Data payload representing course attributes."""

    id: uuid.UUID
    user_id: uuid.UUID
    course_code: str | None = None
    name: str
    description: str | None = None
    instructor: str | None = None
    semester: str | None = None
    academic_year: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class CourseResponse(BaseModel):
    """Standardized API single course success response envelope."""

    success: bool = True
    data: CourseData
    message: str = "Course operation successful."


class CourseListData(BaseModel):
    """Data payload for paginated course listing."""

    items: list[CourseData]
    total: int
    skip: int
    limit: int


class CourseListResponse(BaseModel):
    """Standardized API list response envelope for courses."""

    success: bool = True
    data: CourseListData
    message: str = "Courses retrieved successfully."


class CourseActionResponse(BaseModel):
    """Standardized API response envelope for mutation actions without data payload."""

    success: bool = True
    message: str


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _to_course_data(dto: CourseDTO) -> CourseData:
    """Map domain CourseDTO to presentation CourseData schema."""
    return CourseData(
        id=dto.id,
        user_id=dto.user_id,
        course_code=dto.course_code,
        name=dto.name,
        description=dto.description,
        instructor=dto.instructor,
        semester=dto.semester,
        academic_year=dto.academic_year,
        status=dto.status,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=CourseResponse, status_code=status.HTTP_201_CREATED)
async def create_course(
    payload: CourseCreateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    course_service: Annotated[CourseService, Depends(get_course_service)],
) -> CourseResponse:
    """Create a new academic course belonging strictly to the authenticated student."""
    try:
        dto = await course_service.create_course(
            user_id=current_user.id,
            name=payload.name,
            course_code=payload.course_code,
            description=payload.description,
            instructor=payload.instructor,
            semester=payload.semester,
            academic_year=payload.academic_year,
            status=payload.status,
        )
    except CourseConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except CourseValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except CourseServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return CourseResponse(
        success=True,
        data=_to_course_data(dto),
        message="Course created successfully.",
    )


@router.get("", response_model=CourseListResponse, status_code=status.HTTP_200_OK)
async def list_courses(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    course_service: Annotated[CourseService, Depends(get_course_service)],
    course_status: str | None = Query(
        default=None,
        alias="status",
        description="Filter by course status: 'active' or 'archived'",
    ),
    semester: str | None = Query(
        default=None,
        description="Filter by semester",
    ),
    academic_year: str | None = Query(
        default=None,
        description="Filter by academic year",
    ),
    skip: int = Query(
        default=0,
        ge=0,
        description="Number of records to skip for pagination",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=100,
        description="Maximum number of records to return",
    ),
) -> CourseListResponse:
    """Retrieve paginated list of courses belonging exclusively to the authenticated student."""
    try:
        items, total = await course_service.list_courses(
            user_id=current_user.id,
            status=course_status,
            semester=semester,
            academic_year=academic_year,
            skip=skip,
            limit=limit,
        )
    except CourseValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return CourseListResponse(
        success=True,
        data=CourseListData(
            items=[_to_course_data(c) for c in items],
            total=total,
            skip=skip,
            limit=limit,
        ),
        message="Courses retrieved successfully.",
    )


@router.get("/{course_id}", response_model=CourseResponse, status_code=status.HTTP_200_OK)
async def get_course(
    course_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    course_service: Annotated[CourseService, Depends(get_course_service)],
) -> CourseResponse:
    """Retrieve a single course by ID. Returns 404 if not found or belongs to another user."""
    try:
        dto = await course_service.get_course(
            course_id=course_id,
            user_id=current_user.id,
        )
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc

    return CourseResponse(
        success=True,
        data=_to_course_data(dto),
        message="Course retrieved successfully.",
    )


@router.put("/{course_id}", response_model=CourseResponse, status_code=status.HTTP_200_OK)
async def update_course_put(
    course_id: uuid.UUID,
    payload: CourseUpdateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    course_service: Annotated[CourseService, Depends(get_course_service)],
) -> CourseResponse:
    """Update course attributes via PUT."""
    update_data = payload.model_dump(exclude_unset=True)
    try:
        dto = await course_service.update_course(
            course_id=course_id,
            user_id=current_user.id,
            **update_data,
        )
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc
    except CourseConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except CourseValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except CourseServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return CourseResponse(
        success=True,
        data=_to_course_data(dto),
        message="Course updated successfully.",
    )


@router.patch("/{course_id}", response_model=CourseResponse, status_code=status.HTTP_200_OK)
async def update_course_patch(
    course_id: uuid.UUID,
    payload: CourseUpdateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    course_service: Annotated[CourseService, Depends(get_course_service)],
) -> CourseResponse:
    """Partially update course attributes via PATCH."""
    update_data = payload.model_dump(exclude_unset=True)
    try:
        dto = await course_service.update_course(
            course_id=course_id,
            user_id=current_user.id,
            **update_data,
        )
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc
    except CourseConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except CourseValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except CourseServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return CourseResponse(
        success=True,
        data=_to_course_data(dto),
        message="Course updated successfully.",
    )


@router.post("/{course_id}/archive", response_model=CourseResponse, status_code=status.HTTP_200_OK)
async def archive_course(
    course_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    course_service: Annotated[CourseService, Depends(get_course_service)],
) -> CourseResponse:
    """Archive an academic course."""
    try:
        dto = await course_service.archive_course(
            course_id=course_id,
            user_id=current_user.id,
        )
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc

    return CourseResponse(
        success=True,
        data=_to_course_data(dto),
        message="Course archived successfully.",
    )


@router.delete("/{course_id}", response_model=CourseActionResponse, status_code=status.HTTP_200_OK)
async def delete_course(
    course_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    course_service: Annotated[CourseService, Depends(get_course_service)],
) -> CourseActionResponse:
    """Permanently delete a course owned by the authenticated student."""
    try:
        await course_service.delete_course(
            course_id=course_id,
            user_id=current_user.id,
        )
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc

    return CourseActionResponse(
        success=True,
        message="Course deleted successfully.",
    )
