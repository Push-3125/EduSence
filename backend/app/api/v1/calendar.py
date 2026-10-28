"""Consolidated Academic Calendar API router (US 2.8)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.security import get_current_active_user
from app.services.auth_service import UserDTO
from app.services.calendar_service import (
    CalendarAggregatedDTO,
    CalendarEventDTO,
    CalendarService,
    CalendarValidationError,
    CourseNotFoundError,
    get_calendar_service,
)

router = APIRouter(prefix="/api/v1/calendar", tags=["calendar"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CalendarEventResponse(BaseModel):
    """Schema representing an individual academic calendar event."""

    id: uuid.UUID
    event_type: str = Field(
        ...,
        description="Event type: 'class_session', 'assignment', or 'exam'",
    )
    title: str = Field(..., description="Event title or course name")
    course_id: uuid.UUID = Field(..., description="Associated course UUID")
    course_name: str = Field(..., description="Associated course name")
    course_code: str | None = Field(
        default=None,
        description="Course code (e.g. CS101)",
    )
    start_time: datetime = Field(..., description="Event start datetime (UTC)")
    end_time: datetime | None = Field(
        default=None,
        description="Event end datetime (UTC) if applicable",
    )
    location: str | None = Field(
        default=None,
        description="Classroom, hall, or location URL",
    )
    status: str | None = Field(
        default=None,
        description="Event status if applicable ('scheduled', 'pending', etc.)",
    )
    priority: str | None = Field(
        default=None,
        description="Event priority if applicable ('low', 'medium', 'high')",
    )
    description: str | None = Field(
        default=None,
        description="Event description, notes, or instructions",
    )


# Alias for compatibility if needed
CalendarEventData = CalendarEventResponse


class CalendarData(BaseModel):
    """Data payload schema containing aggregated calendar events across a date range."""

    from_date: datetime
    to_date: datetime
    total_events: int
    events: list[CalendarEventResponse]


class CalendarResponse(BaseModel):
    """Standardized API response envelope for academic calendar events."""

    success: bool = True
    data: CalendarData
    message: str = "Calendar events retrieved successfully."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_event_response(dto: CalendarEventDTO) -> CalendarEventResponse:
    """Map domain CalendarEventDTO to presentation CalendarEventResponse schema."""
    return CalendarEventResponse(
        id=dto.id,
        event_type=dto.event_type,
        title=dto.title,
        course_id=dto.course_id,
        course_name=dto.course_name,
        course_code=dto.course_code,
        start_time=dto.start_time,
        end_time=dto.end_time,
        location=dto.location,
        status=dto.status,
        priority=dto.priority,
        description=dto.description,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=CalendarResponse)
async def get_calendar(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    calendar_service: Annotated[CalendarService, Depends(get_calendar_service)],
    from_date: Annotated[
        datetime | None,
        Query(description="Calendar window start datetime (ISO-8601, inclusive)"),
    ] = None,
    to_date: Annotated[
        datetime | None,
        Query(description="Calendar window end datetime (ISO-8601, inclusive)"),
    ] = None,
    course_id: Annotated[
        uuid.UUID | None,
        Query(description="Optional course UUID filter"),
    ] = None,
    event_types: Annotated[
        str | None,
        Query(
            description=(
                "Optional comma-separated event types filter "
                "(e.g. 'class_session,assignment,exam')"
            )
        ),
    ] = None,
) -> CalendarResponse:
    """Retrieve consolidated academic calendar events for the authenticated student.

    Aggregates class sessions, assignment deadlines, and exams scheduled within the specified
    date window. All events are chronologically sorted and scoped strictly to the student's
    owned courses.

    Args:
        current_user: Authenticated student user.
        calendar_service: Injected CalendarService instance.
        from_date: Start datetime bound. Defaults to beginning of current month.
        to_date: End datetime bound. Defaults to end of current month.
        course_id: Optional course filter.
        event_types: Optional comma-delimited filter of event types.

    Returns:
        CalendarResponse containing uniform events list and count.

    Raises:
        HTTPException: 404 if course_id is not found or unowned, 422 if parameters are invalid.
    """
    now = datetime.now(UTC)
    resolved_from: datetime
    resolved_to: datetime
    if from_date is None and to_date is None:
        resolved_from = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # End of current month
        next_month = (resolved_from.replace(day=28) + timedelta(days=4)).replace(day=1)
        resolved_to = next_month - timedelta(seconds=1)
    elif from_date is None:
        assert to_date is not None
        resolved_from = to_date - timedelta(days=30)
        resolved_to = to_date
    elif to_date is None:
        resolved_from = from_date
        resolved_to = from_date + timedelta(days=30)
    else:
        resolved_from = from_date
        resolved_to = to_date

    parsed_types: list[str] | None = None
    if event_types is not None:
        cleaned_types = [t.strip().lower() for t in event_types.split(",") if t.strip()]
        if cleaned_types:
            parsed_types = cleaned_types

    try:
        dto: CalendarAggregatedDTO = await calendar_service.get_aggregated_calendar(
            user_id=current_user.id,
            from_date=resolved_from,
            to_date=resolved_to,
            course_id=course_id,
            event_types=parsed_types,
        )
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except CalendarValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    return CalendarResponse(
        success=True,
        data=CalendarData(
            from_date=dto.from_date,
            to_date=dto.to_date,
            total_events=dto.total_events,
            events=[_to_event_response(e) for e in dto.events],
        ),
        message="Calendar events retrieved successfully.",
    )
