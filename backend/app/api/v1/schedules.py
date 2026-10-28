"""Weekly Recurring Class Schedules and Sessions API router (US 2.9)."""

import uuid
from datetime import date, datetime, time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.core.security import get_current_active_user
from app.services.auth_service import UserDTO
from app.services.schedule_service import (
    CourseNotFoundError,
    ScheduleDTO,
    ScheduleNotFoundError,
    ScheduleService,
    ScheduleValidationError,
    SessionDTO,
    SessionNotFoundError,
    get_schedule_service,
)

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ScheduleCreateRequest(BaseModel):
    """Payload schema for creating a weekly recurring class schedule."""

    course_id: uuid.UUID = Field(..., description="Target course UUID")
    day_of_week: int = Field(
        ...,
        ge=0,
        le=6,
        description="Day of week (0=Monday, 1=Tuesday, ..., 6=Sunday)",
    )
    start_time: time = Field(
        ...,
        description="Daily class start time (HH:MM:SS or HH:MM)",
    )
    end_time: time = Field(
        ...,
        description="Daily class end time (HH:MM:SS or HH:MM, must be > start_time)",
    )
    start_date: date = Field(
        ...,
        description="Schedule start date (YYYY-MM-DD)",
    )
    end_date: date = Field(
        ...,
        description="Schedule end date (YYYY-MM-DD, must be >= start_date)",
    )
    location: str | None = Field(
        default=None,
        max_length=255,
        description="Physical classroom, hall, or online link",
    )

    @field_validator("location")
    @classmethod
    def strip_location(cls, value: str | None) -> str | None:
        """Strip whitespace from optional location."""
        if value is not None:
            stripped = value.strip()
            return stripped or None
        return None

    @model_validator(mode="after")
    def validate_time_and_date_ranges(self) -> "ScheduleCreateRequest":
        """Enforce end_time > start_time and end_date >= start_date."""
        if self.end_time <= self.start_time:
            raise PydanticCustomError(
                "value_error",
                "end_time must be strictly after start_time.",
            )
        if self.end_date < self.start_date:
            raise PydanticCustomError(
                "value_error",
                "end_date must be greater than or equal to start_date.",
            )
        return self


class ScheduleData(BaseModel):
    """Data payload representing schedule attributes."""

    id: uuid.UUID
    course_id: uuid.UUID
    day_of_week: int
    start_time: time
    end_time: time
    start_date: date
    end_date: date
    location: str | None = None
    created_at: datetime
    updated_at: datetime


class ScheduleResponse(BaseModel):
    """Standardized API single schedule response envelope."""

    success: bool = True
    data: ScheduleData
    message: str = "Class schedule operation successful."


class ScheduleListData(BaseModel):
    """Data payload for paginated schedule listing."""

    items: list[ScheduleData]
    total: int
    skip: int
    limit: int


class ScheduleListResponse(BaseModel):
    """Standardized API list response envelope for class schedules."""

    success: bool = True
    data: ScheduleListData
    message: str = "Class schedules retrieved successfully."


class SessionData(BaseModel):
    """Data payload representing an individual class session instance."""

    id: uuid.UUID
    schedule_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    location: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class SessionResponse(BaseModel):
    """Standardized API single session response envelope."""

    success: bool = True
    data: SessionData
    message: str = "Class session operation successful."


class SessionListData(BaseModel):
    """Data payload for class session instances."""

    items: list[SessionData]
    total: int


class SessionListResponse(BaseModel):
    """Standardized API list response envelope for class sessions."""

    success: bool = True
    data: SessionListData
    message: str = "Class sessions retrieved successfully."


class SessionStatusUpdateRequest(BaseModel):
    """Payload schema for updating a class session status."""

    status: str = Field(
        ...,
        description="Target status: 'scheduled', 'completed', or 'cancelled'",
    )

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        """Validate and normalize session status."""
        normalized = value.strip().lower()
        if normalized not in ("scheduled", "completed", "cancelled"):
            raise PydanticCustomError(
                "value_error",
                "Status must be one of: 'scheduled', 'completed', 'cancelled'.",
            )
        return normalized


class ScheduleActionResponse(BaseModel):
    """Standardized API response envelope for mutation actions without data payload."""

    success: bool = True
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_schedule_data(dto: ScheduleDTO) -> ScheduleData:
    """Map domain ScheduleDTO to presentation ScheduleData schema."""
    return ScheduleData(
        id=dto.id,
        course_id=dto.course_id,
        day_of_week=dto.day_of_week,
        start_time=dto.start_time,
        end_time=dto.end_time,
        start_date=dto.start_date,
        end_date=dto.end_date,
        location=dto.location,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
    )


def _to_session_data(dto: SessionDTO) -> SessionData:
    """Map domain SessionDTO to presentation SessionData schema."""
    return SessionData(
        id=dto.id,
        schedule_id=dto.schedule_id,
        starts_at=dto.starts_at,
        ends_at=dto.ends_at,
        location=dto.location,
        status=dto.status,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# Note: `/sessions` endpoints are declared BEFORE `/{schedule_id}` to prevent
# FastAPI routing collisions with UUID path parameters.
# ---------------------------------------------------------------------------


@router.post("", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    payload: ScheduleCreateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    schedule_service: Annotated[ScheduleService, Depends(get_schedule_service)],
) -> ScheduleResponse:
    """Create a weekly recurring class schedule and generate concrete session instances."""
    try:
        dto = await schedule_service.create_schedule(
            user_id=current_user.id,
            course_id=payload.course_id,
            day_of_week=payload.day_of_week,
            start_time=payload.start_time,
            end_time=payload.end_time,
            start_date=payload.start_date,
            end_date=payload.end_date,
            location=payload.location,
        )
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc
    except ScheduleValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return ScheduleResponse(
        success=True,
        data=_to_schedule_data(dto),
        message="Class schedule created successfully.",
    )


@router.get("", response_model=ScheduleListResponse, status_code=status.HTTP_200_OK)
async def list_schedules(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    schedule_service: Annotated[ScheduleService, Depends(get_schedule_service)],
    course_id: Annotated[
        uuid.UUID | None,
        Query(description="Filter schedules by course UUID"),
    ] = None,
    skip: Annotated[
        int,
        Query(ge=0, description="Offset count for pagination"),
    ] = 0,
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Maximum items per page"),
    ] = 50,
) -> ScheduleListResponse:
    """List class schedules for the authenticated student."""
    try:
        dtos, total = await schedule_service.list_schedules(
            user_id=current_user.id,
            course_id=course_id,
            skip=skip,
            limit=limit,
        )
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc
    except ScheduleValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return ScheduleListResponse(
        success=True,
        data=ScheduleListData(
            items=[_to_schedule_data(dto) for dto in dtos],
            total=total,
            skip=skip,
            limit=limit,
        ),
        message="Class schedules retrieved successfully.",
    )


@router.get("/sessions", response_model=SessionListResponse, status_code=status.HTTP_200_OK)
async def list_sessions(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    schedule_service: Annotated[ScheduleService, Depends(get_schedule_service)],
    schedule_id: Annotated[
        uuid.UUID | None,
        Query(description="Filter sessions by class schedule UUID"),
    ] = None,
    from_date: Annotated[
        str | None,
        Query(description="Filter sessions on or after ISO timestamp or date (YYYY-MM-DD)"),
    ] = None,
    to_date: Annotated[
        str | None,
        Query(description="Filter sessions on or before ISO timestamp or date (YYYY-MM-DD)"),
    ] = None,
    session_status: Annotated[
        str | None,
        Query(alias="status", description="Filter sessions by status: 'scheduled', 'completed', 'cancelled'"),
    ] = None,
) -> SessionListResponse:
    """List individual class session instances with date range and status filters."""
    parsed_from: datetime | date | None = None
    if from_date:
        try:
            parsed_from = datetime.fromisoformat(from_date)
        except ValueError:
            try:
                parsed_from = date.fromisoformat(from_date)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid from_date format: '{from_date}'. Use ISO-8601.",
                ) from exc

    parsed_to: datetime | date | None = None
    if to_date:
        try:
            parsed_to = datetime.fromisoformat(to_date)
        except ValueError:
            try:
                parsed_to = date.fromisoformat(to_date)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid to_date format: '{to_date}'. Use ISO-8601.",
                ) from exc

    try:
        dtos = await schedule_service.list_sessions(
            user_id=current_user.id,
            schedule_id=schedule_id,
            from_date=parsed_from,
            to_date=parsed_to,
            status=session_status,
        )
    except ScheduleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Class schedule not found.",
        ) from exc
    except ScheduleValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return SessionListResponse(
        success=True,
        data=SessionListData(
            items=[_to_session_data(dto) for dto in dtos],
            total=len(dtos),
        ),
        message="Class sessions retrieved successfully.",
    )


@router.patch("/sessions/{session_id}", response_model=SessionResponse, status_code=status.HTTP_200_OK)
async def update_session_status(
    session_id: uuid.UUID,
    payload: SessionStatusUpdateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    schedule_service: Annotated[ScheduleService, Depends(get_schedule_service)],
) -> SessionResponse:
    """Update status of an individual class session (e.g., mark as 'cancelled' or 'completed')."""
    try:
        dto = await schedule_service.update_session_status(
            session_id=session_id,
            user_id=current_user.id,
            status=payload.status,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Class session not found.",
        ) from exc
    except ScheduleValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return SessionResponse(
        success=True,
        data=_to_session_data(dto),
        message="Class session updated successfully.",
    )


@router.get("/{schedule_id}", response_model=ScheduleResponse, status_code=status.HTTP_200_OK)
async def get_schedule(
    schedule_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    schedule_service: Annotated[ScheduleService, Depends(get_schedule_service)],
) -> ScheduleResponse:
    """Fetch details of a single recurring class schedule."""
    try:
        dto = await schedule_service.get_schedule(
            schedule_id=schedule_id,
            user_id=current_user.id,
        )
    except ScheduleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Class schedule not found.",
        ) from exc

    return ScheduleResponse(
        success=True,
        data=_to_schedule_data(dto),
        message="Class schedule retrieved successfully.",
    )


@router.delete("/{schedule_id}", response_model=ScheduleActionResponse, status_code=status.HTTP_200_OK)
async def delete_schedule(
    schedule_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    schedule_service: Annotated[ScheduleService, Depends(get_schedule_service)],
) -> ScheduleActionResponse:
    """Permanently delete a class schedule and cascade all associated sessions."""
    try:
        await schedule_service.delete_schedule(
            schedule_id=schedule_id,
            user_id=current_user.id,
        )
    except ScheduleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Class schedule not found.",
        ) from exc

    return ScheduleActionResponse(
        success=True,
        message="Class schedule deleted successfully.",
    )


__all__ = ["router"]
