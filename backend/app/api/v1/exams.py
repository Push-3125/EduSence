"""Exams CRUD API router for academic exam schedule tracking (US 2.7)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

from app.core.security import get_current_active_user
from app.services.auth_service import UserDTO
from app.services.exam_service import (
    _UNSET,
    CourseNotFoundError,
    ExamDTO,
    ExamNotFoundError,
    ExamService,
    ExamValidationError,
    get_exam_service,
)

router = APIRouter(prefix="/api/v1/exams", tags=["exams"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ExamCreateRequest(BaseModel):
    """Payload schema for creating a new exam schedule."""

    course_id: uuid.UUID = Field(..., description="Target course UUID")
    title: str = Field(
        ..., min_length=1, max_length=255, description="Exam title (e.g., Midterm Exam)"
    )
    exam_at: datetime = Field(
        ..., description="Scheduled exam date and time with timezone"
    )
    duration_minutes: int | None = Field(
        default=None, gt=0, description="Exam duration in minutes (must be > 0)"
    )
    location: str | None = Field(
        default=None, max_length=255, description="Room number, hall, or online link"
    )
    notes: str | None = Field(
        default=None, description="Detailed topics, materials allowed, or instructions"
    )

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Ensure title is not empty or composed solely of whitespace."""
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "value_error", "Exam title cannot be empty or whitespace."
            )
        return stripped

    @field_validator("location")
    @classmethod
    def strip_location(cls, value: str | None) -> str | None:
        """Strip whitespace from optional location."""
        if value is not None:
            stripped = value.strip()
            return stripped or None
        return None

    @field_validator("notes")
    @classmethod
    def strip_notes(cls, value: str | None) -> str | None:
        """Strip whitespace from optional notes."""
        if value is not None:
            stripped = value.strip()
            return stripped or None
        return None


class ExamUpdateRequest(BaseModel):
    """Payload schema for updating existing exam attributes."""

    course_id: uuid.UUID | None = Field(
        default=None, description="Reassigned course UUID"
    )
    title: str | None = Field(
        default=None, min_length=1, max_length=255, description="Exam title"
    )
    exam_at: datetime | None = Field(
        default=None, description="Scheduled exam date and time with timezone"
    )
    duration_minutes: int | None = Field(
        default=None, gt=0, description="Exam duration in minutes (must be > 0)"
    )
    location: str | None = Field(
        default=None, max_length=255, description="Room number, hall, or online link"
    )
    notes: str | None = Field(
        default=None, description="Detailed topics, materials allowed, or instructions"
    )

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        """Ensure title is not empty or whitespace if provided."""
        if value is not None:
            stripped = value.strip()
            if not stripped:
                raise PydanticCustomError(
                    "value_error", "Exam title cannot be empty or whitespace."
                )
            return stripped
        return None

    @field_validator("location")
    @classmethod
    def strip_location(cls, value: str | None) -> str | None:
        """Strip whitespace from optional location."""
        if value is not None:
            stripped = value.strip()
            return stripped or None
        return None

    @field_validator("notes")
    @classmethod
    def strip_notes(cls, value: str | None) -> str | None:
        """Strip whitespace from optional notes."""
        if value is not None:
            stripped = value.strip()
            return stripped or None
        return None


class ExamData(BaseModel):
    """Data payload representing exam attributes."""

    id: uuid.UUID
    course_id: uuid.UUID
    title: str
    exam_at: datetime
    duration_minutes: int | None = None
    location: str | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class ExamResponse(BaseModel):
    """Standardized API single exam response envelope."""

    success: bool = True
    data: ExamData
    message: str = "Exam operation successful."


class ExamListData(BaseModel):
    """Data payload for paginated exam listing."""

    items: list[ExamData]
    total: int
    skip: int
    limit: int


class ExamListResponse(BaseModel):
    """Standardized API list response envelope for exams."""

    success: bool = True
    data: ExamListData
    message: str = "Exams retrieved successfully."


class ExamActionResponse(BaseModel):
    """Standardized API response envelope for mutation actions without data payload."""

    success: bool = True
    message: str


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _to_exam_data(dto: ExamDTO) -> ExamData:
    """Map domain ExamDTO to presentation ExamData schema."""
    return ExamData(
        id=dto.id,
        course_id=dto.course_id,
        title=dto.title,
        exam_at=dto.exam_at,
        duration_minutes=dto.duration_minutes,
        location=dto.location,
        notes=dto.notes,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=ExamResponse, status_code=status.HTTP_201_CREATED)
async def create_exam(
    payload: ExamCreateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    exam_service: Annotated[ExamService, Depends(get_exam_service)],
) -> ExamResponse:
    """Create a new exam schedule under a course owned by the authenticated student."""
    try:
        dto = await exam_service.create_exam(
            user_id=current_user.id,
            course_id=payload.course_id,
            title=payload.title,
            exam_at=payload.exam_at,
            duration_minutes=payload.duration_minutes,
            location=payload.location,
            notes=payload.notes,
        )
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc
    except ExamValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return ExamResponse(
        success=True,
        data=_to_exam_data(dto),
        message="Exam created successfully.",
    )


@router.get("", response_model=ExamListResponse, status_code=status.HTTP_200_OK)
async def list_exams(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    exam_service: Annotated[ExamService, Depends(get_exam_service)],
    course_id: Annotated[
        uuid.UUID | None,
        Query(description="Filter exams by course UUID"),
    ] = None,
    upcoming_only: Annotated[
        bool,
        Query(description="Filter for upcoming exams only (exam_at >= now)"),
    ] = False,
    skip: Annotated[
        int,
        Query(ge=0, description="Offset count for pagination"),
    ] = 0,
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Maximum items per page"),
    ] = 50,
) -> ExamListResponse:
    """List paginated exams for the authenticated student with optional filters."""
    try:
        dtos, total = await exam_service.list_exams(
            user_id=current_user.id,
            course_id=course_id,
            upcoming_only=upcoming_only,
            skip=skip,
            limit=limit,
        )
    except ExamValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return ExamListResponse(
        success=True,
        data=ExamListData(
            items=[_to_exam_data(dto) for dto in dtos],
            total=total,
            skip=skip,
            limit=limit,
        ),
        message="Exams retrieved successfully.",
    )


@router.get("/{exam_id}", response_model=ExamResponse, status_code=status.HTTP_200_OK)
async def get_exam(
    exam_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    exam_service: Annotated[ExamService, Depends(get_exam_service)],
) -> ExamResponse:
    """Retrieve details of a single exam owned by the student."""
    try:
        dto = await exam_service.get_exam(
            exam_id=exam_id,
            user_id=current_user.id,
        )
    except ExamNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Exam not found.",
        ) from exc

    return ExamResponse(
        success=True,
        data=_to_exam_data(dto),
        message="Exam retrieved successfully.",
    )


@router.put("/{exam_id}", response_model=ExamResponse, status_code=status.HTTP_200_OK)
async def update_exam(
    exam_id: uuid.UUID,
    payload: ExamUpdateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    exam_service: Annotated[ExamService, Depends(get_exam_service)],
) -> ExamResponse:
    """Update an exam owned by the authenticated student."""
    try:
        dto = await exam_service.update_exam(
            exam_id=exam_id,
            user_id=current_user.id,
            course_id=payload.course_id if "course_id" in payload.model_fields_set else _UNSET,
            title=payload.title if "title" in payload.model_fields_set else _UNSET,
            exam_at=payload.exam_at if "exam_at" in payload.model_fields_set else _UNSET,
            duration_minutes=payload.duration_minutes if "duration_minutes" in payload.model_fields_set else _UNSET,
            location=payload.location if "location" in payload.model_fields_set else _UNSET,
            notes=payload.notes if "notes" in payload.model_fields_set else _UNSET,
        )
    except ExamNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Exam not found.",
        ) from exc
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc
    except ExamValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return ExamResponse(
        success=True,
        data=_to_exam_data(dto),
        message="Exam updated successfully.",
    )


@router.patch("/{exam_id}", response_model=ExamResponse, status_code=status.HTTP_200_OK)
async def patch_exam(
    exam_id: uuid.UUID,
    payload: ExamUpdateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    exam_service: Annotated[ExamService, Depends(get_exam_service)],
) -> ExamResponse:
    """Partially update an exam owned by the authenticated student."""
    return await update_exam(
        exam_id=exam_id,
        payload=payload,
        current_user=current_user,
        exam_service=exam_service,
    )


@router.delete("/{exam_id}", response_model=ExamActionResponse, status_code=status.HTTP_200_OK)
async def delete_exam(
    exam_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    exam_service: Annotated[ExamService, Depends(get_exam_service)],
) -> ExamActionResponse:
    """Permanently delete an exam owned by the authenticated student."""
    try:
        await exam_service.delete_exam(
            exam_id=exam_id,
            user_id=current_user.id,
        )
    except ExamNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Exam not found.",
        ) from exc

    return ExamActionResponse(
        success=True,
        message="Exam deleted successfully.",
    )
