"""Schedule service handling recurring class schedules, sessions generation, and student isolation (US 2.9)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import Depends

from app.repositories.course_repo import CourseRepository, get_course_repo
from app.repositories.schedule_repo import (
    ClassSchedule,
    ClassSession,
    ScheduleRepository,
    get_schedule_repo,
)
from app.services.course_service import CourseNotFoundError


class ScheduleServiceError(Exception):
    """Base exception for schedule service domain errors."""


class ScheduleNotFoundError(ScheduleServiceError):
    """Raised when a class schedule is not found or does not belong to the user."""


class SessionNotFoundError(ScheduleServiceError):
    """Raised when a class session is not found or does not belong to the user."""


class ScheduleValidationError(ScheduleServiceError):
    """Raised when schedule or session parameters violate domain validation rules."""


@dataclass
class ScheduleDTO:
    """Decoupled Data Transfer Object representing a recurring class schedule."""

    id: uuid.UUID
    course_id: uuid.UUID
    day_of_week: int
    start_time: time
    end_time: time
    start_date: date
    end_date: date
    location: str | None
    created_at: datetime
    updated_at: datetime


@dataclass
class SessionDTO:
    """Decoupled Data Transfer Object representing an individual class session instance."""

    id: uuid.UUID
    schedule_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    location: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class ScheduleService:
    """Service encapsulating weekly recurring class schedule lifecycle and student ownership."""

    def __init__(
        self,
        schedule_repo: ScheduleRepository,
        course_repo: CourseRepository,
    ) -> None:
        self.schedule_repo = schedule_repo
        self.course_repo = course_repo

    @staticmethod
    def _to_schedule_dto(model: ClassSchedule) -> ScheduleDTO:
        """Map ClassSchedule ORM model to decoupled ScheduleDTO."""
        return ScheduleDTO(
            id=model.id,
            course_id=model.course_id,
            day_of_week=model.day_of_week,
            start_time=model.start_time,
            end_time=model.end_time,
            start_date=model.start_date,
            end_date=model.end_date,
            location=model.location,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @staticmethod
    def _to_session_dto(model: ClassSession) -> SessionDTO:
        """Map ClassSession ORM model to decoupled SessionDTO."""
        return SessionDTO(
            id=model.id,
            schedule_id=model.schedule_id,
            starts_at=model.starts_at,
            ends_at=model.ends_at,
            location=model.location,
            status=model.status,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def create_schedule(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID,
        day_of_week: int,
        start_time: time,
        end_time: time,
        start_date: date,
        end_date: date,
        location: str | None = None,
    ) -> ScheduleDTO:
        """Create a weekly recurring class schedule and generate concrete session instances.

        Args:
            user_id: Authenticated user UUID.
            course_id: Target course UUID (must belong to user_id).
            day_of_week: Day of week index (0=Monday, 6=Sunday).
            start_time: Daily class start time.
            end_time: Daily class end time.
            start_date: Term/schedule start date.
            end_date: Term/schedule end date.
            location: Optional physical room or meeting URL.

        Returns:
            Created ScheduleDTO.

        Raises:
            CourseNotFoundError: If course does not exist or is not owned by user_id.
            ScheduleValidationError: If timing or dates violate domain rules.
        """
        course = await self.course_repo.get_by_id_and_user(course_id, user_id)
        if not course:
            raise CourseNotFoundError("Course not found or access denied.")

        if not (0 <= day_of_week <= 6):
            raise ScheduleValidationError(
                f"day_of_week must be an integer between 0 (Monday) and 6 (Sunday), got: {day_of_week}."
            )

        if end_time <= start_time:
            raise ScheduleValidationError("end_time must be strictly after start_time.")

        if end_date < start_date:
            raise ScheduleValidationError("end_date must be greater than or equal to start_date.")

        cleaned_location = location.strip() if location and location.strip() else None

        schedule = await self.schedule_repo.create_schedule(
            course_id=course_id,
            day_of_week=day_of_week,
            start_time=start_time,
            end_time=end_time,
            start_date=start_date,
            end_date=end_date,
            location=cleaned_location,
        )

        # Generate concrete sessions across [start_date, end_date]
        sessions_data: list[dict[str, Any]] = []
        curr_date = start_date
        while curr_date <= end_date:
            if curr_date.weekday() == day_of_week:
                # Strip tzinfo from time if present to ensure clean combine into UTC
                s_time = start_time.replace(tzinfo=None) if start_time.tzinfo else start_time
                e_time = end_time.replace(tzinfo=None) if end_time.tzinfo else end_time

                starts_at = datetime.combine(curr_date, s_time, tzinfo=UTC)
                ends_at = datetime.combine(curr_date, e_time, tzinfo=UTC)

                sessions_data.append(
                    {
                        "schedule_id": schedule.id,
                        "starts_at": starts_at,
                        "ends_at": ends_at,
                        "location": cleaned_location,
                        "status": "scheduled",
                    }
                )
            curr_date += timedelta(days=1)

        if sessions_data:
            await self.schedule_repo.create_sessions_batch(sessions_data)

        return self._to_schedule_dto(schedule)

    async def get_schedule(
        self,
        schedule_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ScheduleDTO:
        """Retrieve a specific class schedule enforcing student ownership.

        Args:
            schedule_id: Target schedule UUID.
            user_id: Authenticated user UUID.

        Returns:
            ScheduleDTO if found and owned.

        Raises:
            ScheduleNotFoundError: If schedule does not exist or belongs to another user.
        """
        schedule = await self.schedule_repo.get_schedule_by_id_and_user(schedule_id, user_id)
        if not schedule:
            raise ScheduleNotFoundError("Class schedule not found.")
        return self._to_schedule_dto(schedule)

    async def list_schedules(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[ScheduleDTO], int]:
        """List class schedules for the authenticated student.

        Args:
            user_id: Authenticated user UUID.
            course_id: Optional course filter.
            skip: Pagination offset (must be >= 0).
            limit: Page size (1 to 100).

        Returns:
            Tuple of (list of ScheduleDTO, total count).

        Raises:
            CourseNotFoundError: If filter course_id is invalid or unowned.
            ScheduleValidationError: If pagination bounds are invalid.
        """
        if skip < 0:
            raise ScheduleValidationError("skip cannot be negative.")
        if limit < 1 or limit > 100:
            raise ScheduleValidationError("limit must be between 1 and 100.")

        if course_id is not None:
            course = await self.course_repo.get_by_id_and_user(course_id, user_id)
            if not course:
                raise CourseNotFoundError("Course not found or access denied.")

        schedules, total = await self.schedule_repo.list_schedules_by_user(
            user_id=user_id,
            course_id=course_id,
            skip=skip,
            limit=limit,
        )
        return [self._to_schedule_dto(s) for s in schedules], total

    async def list_sessions(
        self,
        user_id: uuid.UUID,
        schedule_id: uuid.UUID | None = None,
        from_date: datetime | date | None = None,
        to_date: datetime | date | None = None,
        status: str | None = None,
    ) -> list[SessionDTO]:
        """List individual class session instances with date range and status filters.

        Args:
            user_id: Authenticated user UUID.
            schedule_id: Optional schedule UUID filter.
            from_date: Optional filter for sessions starting on or after timestamp/date.
            to_date: Optional filter for sessions starting on or before timestamp/date.
            status: Optional filter by status ('scheduled', 'completed', 'cancelled').

        Returns:
            List of matching SessionDTO items ordered chronologically.

        Raises:
            ScheduleNotFoundError: If filtered schedule_id is invalid or unowned.
            ScheduleValidationError: If status or date range is invalid.
        """
        if schedule_id is not None:
            schedule = await self.schedule_repo.get_schedule_by_id_and_user(schedule_id, user_id)
            if not schedule:
                raise ScheduleNotFoundError("Class schedule not found.")

        if status is not None:
            valid_statuses = {"scheduled", "completed", "cancelled"}
            if status not in valid_statuses:
                raise ScheduleValidationError(
                    f"Invalid session status: '{status}'. Allowed statuses: {', '.join(sorted(valid_statuses))}."
                )

        if from_date is not None and to_date is not None:
            f_dt = from_date if isinstance(from_date, datetime) else datetime.combine(from_date, time.min)
            t_dt = to_date if isinstance(to_date, datetime) else datetime.combine(to_date, time.max)
            if t_dt < f_dt:
                raise ScheduleValidationError("to_date cannot be earlier than from_date.")

        sessions = await self.schedule_repo.list_sessions_by_user(
            user_id=user_id,
            schedule_id=schedule_id,
            from_date=from_date,
            to_date=to_date,
            status=status,
        )
        return [self._to_session_dto(s) for s in sessions]

    async def update_session_status(
        self,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        status: str,
    ) -> SessionDTO:
        """Update an individual class session status (e.g. cancelled or completed).

        Args:
            session_id: Target session UUID.
            user_id: Authenticated user UUID.
            status: New status ('scheduled', 'completed', 'cancelled').

        Returns:
            Updated SessionDTO.

        Raises:
            SessionNotFoundError: If session does not exist or is unowned.
            ScheduleValidationError: If status value is invalid.
        """
        valid_statuses = {"scheduled", "completed", "cancelled"}
        if status not in valid_statuses:
            raise ScheduleValidationError(
                f"Invalid session status: '{status}'. Allowed statuses: {', '.join(sorted(valid_statuses))}."
            )

        session = await self.schedule_repo.get_session_by_id_and_user(session_id, user_id)
        if not session:
            raise SessionNotFoundError("Class session not found.")

        updated = await self.schedule_repo.update_session(session, status=status)
        return self._to_session_dto(updated)

    async def delete_schedule(
        self,
        schedule_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Permanently delete a class schedule and cascade all generated sessions.

        Args:
            schedule_id: Target schedule UUID.
            user_id: Authenticated user UUID.

        Raises:
            ScheduleNotFoundError: If schedule does not exist or is unowned.
        """
        schedule = await self.schedule_repo.get_schedule_by_id_and_user(schedule_id, user_id)
        if not schedule:
            raise ScheduleNotFoundError("Class schedule not found.")

        await self.schedule_repo.delete_schedule(schedule)


def get_schedule_service(
    schedule_repo: Annotated[ScheduleRepository, Depends(get_schedule_repo)],
    course_repo: Annotated[CourseRepository, Depends(get_course_repo)],
) -> ScheduleService:
    """FastAPI dependency provider for ScheduleService."""
    return ScheduleService(
        schedule_repo=schedule_repo,
        course_repo=course_repo,
    )


__all__ = [
    "CourseNotFoundError",
    "ScheduleDTO",
    "ScheduleNotFoundError",
    "ScheduleService",
    "ScheduleServiceError",
    "ScheduleValidationError",
    "SessionDTO",
    "SessionNotFoundError",
    "get_schedule_service",
]
