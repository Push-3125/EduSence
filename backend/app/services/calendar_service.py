"""Consolidated Academic Calendar Service aggregating class sessions, assignments, and exams (US 2.8)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import Depends

from app.repositories.assignment_repo import (
    Assignment,
    AssignmentRepository,
    get_assignment_repo,
)
from app.repositories.course_repo import (
    Course,
    CourseRepository,
    get_course_repo,
)
from app.repositories.exam_repo import (
    Exam,
    ExamRepository,
    get_exam_repo,
)
from app.repositories.schedule_repo import (
    ClassSchedule,
    ClassSession,
    ScheduleRepository,
    get_schedule_repo,
)
from app.services.course_service import CourseNotFoundError

CalendarEventType = Literal["class_session", "assignment", "exam"]
VALID_EVENT_TYPES: set[str] = {"class_session", "assignment", "exam"}


class CalendarServiceError(Exception):
    """Base exception for calendar service domain errors."""


class CalendarValidationError(CalendarServiceError):
    """Raised when calendar query parameters or date ranges violate domain validation rules."""


@dataclass
class CalendarEventDTO:
    """Decoupled Data Transfer Object representing a single academic calendar event."""

    id: uuid.UUID
    event_type: str  # "class_session", "assignment", "exam"
    title: str
    course_id: uuid.UUID
    course_name: str
    course_code: str | None
    start_time: datetime
    end_time: datetime | None
    location: str | None
    status: str | None
    priority: str | None
    description: str | None


@dataclass
class CalendarAggregatedDTO:
    """Decoupled Data Transfer Object representing aggregated calendar events across a date range."""

    from_date: datetime
    to_date: datetime
    total_events: int
    events: list[CalendarEventDTO]


class CalendarService:
    """Service encapsulating calendar aggregation logic with strict student ownership isolation."""

    def __init__(
        self,
        course_repo: CourseRepository,
        assignment_repo: AssignmentRepository,
        exam_repo: ExamRepository,
        schedule_repo: ScheduleRepository,
    ) -> None:
        self.course_repo = course_repo
        self.assignment_repo = assignment_repo
        self.exam_repo = exam_repo
        self.schedule_repo = schedule_repo

    async def get_aggregated_calendar(
        self,
        user_id: uuid.UUID,
        from_date: datetime,
        to_date: datetime,
        course_id: uuid.UUID | None = None,
        event_types: list[str] | None = None,
    ) -> CalendarAggregatedDTO:
        """Aggregate all academic events for the authenticated student within [from_date, to_date].

        Args:
            user_id: Authenticated student UUID.
            from_date: Starting datetime boundary (inclusive).
            to_date: Ending datetime boundary (inclusive).
            course_id: Optional course filter.
            event_types: Optional list of event types to include ('class_session', 'assignment', 'exam').

        Returns:
            CalendarAggregatedDTO with chronologically ordered uniform events.

        Raises:
            CalendarValidationError: If to_date < from_date or invalid event_types requested.
            CourseNotFoundError: If course_id is specified but not found/owned by user_id.
        """
        # Ensure UTC timezone awareness
        from_dt = from_date if from_date.tzinfo else from_date.replace(tzinfo=UTC)
        to_dt = to_date if to_date.tzinfo else to_date.replace(tzinfo=UTC)

        if to_dt < from_dt:
            raise CalendarValidationError("to_date cannot be earlier than from_date.")

        # Validate event_types if provided
        active_types: set[str]
        if event_types is not None:
            active_types = set()
            for et in event_types:
                cleaned = et.strip().lower()
                if cleaned not in VALID_EVENT_TYPES:
                    raise CalendarValidationError(
                        f"Invalid event_type: '{et}'. Allowed types: {', '.join(sorted(VALID_EVENT_TYPES))}."
                    )
                active_types.add(cleaned)
        else:
            active_types = VALID_EVENT_TYPES.copy()

        # Retrieve student's courses with strict ownership check
        courses_map: dict[uuid.UUID, Course] = {}
        if course_id is not None:
            course = await self.course_repo.get_by_id_and_user(course_id, user_id)
            if not course:
                raise CourseNotFoundError("Course not found or access denied.")
            courses_map[course.id] = course
        else:
            if hasattr(self.course_repo, "list_all_by_user"):
                courses = await self.course_repo.list_all_by_user(user_id)
            else:
                courses_res = await self.course_repo.list_by_user(user_id=user_id, limit=1000)
                courses = courses_res[0] if isinstance(courses_res, tuple) else courses_res
            courses_map = {c.id: c for c in courses}

        # If student has no enrolled courses, return 0 events immediately
        if not courses_map:
            return CalendarAggregatedDTO(
                from_date=from_dt,
                to_date=to_dt,
                total_events=0,
                events=[],
            )

        aggregated_events: list[CalendarEventDTO] = []

        # -------------------------------------------------------------------
        # 1. Class Sessions
        # -------------------------------------------------------------------
        if "class_session" in active_types:
            sessions: list[ClassSession] = []
            if hasattr(self.schedule_repo, "list_sessions_by_date_range"):
                sessions = await self.schedule_repo.list_sessions_by_date_range(
                    user_id=user_id,
                    from_date=from_dt,
                    to_date=to_dt,
                    course_id=course_id,
                )
            elif hasattr(self.schedule_repo, "list_sessions_by_user"):
                try:
                    sessions = await self.schedule_repo.list_sessions_by_user(
                        user_id=user_id,
                        from_date=from_dt,
                        to_date=to_dt,
                        course_id=course_id,
                    )
                except TypeError:
                    sessions = await self.schedule_repo.list_sessions_by_user(
                        user_id=user_id,
                        from_date=from_dt,
                        to_date=to_dt,
                    )

            schedules_cache: dict[uuid.UUID, ClassSchedule | None] = {}
            for s in sessions:
                s_start = s.starts_at if s.starts_at.tzinfo else s.starts_at.replace(tzinfo=UTC)
                if not (from_dt <= s_start <= to_dt):
                    continue

                # Determine course_id for this session
                c_id = getattr(s, "course_id", None)
                sched = getattr(s, "schedule", None)
                if not c_id:
                    if sched and getattr(sched, "course_id", None):
                        c_id = sched.course_id
                    elif hasattr(self.schedule_repo, "get_schedule_by_id"):
                        if s.schedule_id not in schedules_cache:
                            schedules_cache[s.schedule_id] = await self.schedule_repo.get_schedule_by_id(s.schedule_id)
                        cached_sched = schedules_cache.get(s.schedule_id)
                        if cached_sched:
                            c_id = cached_sched.course_id
                            if not sched:
                                sched = cached_sched

                if not c_id or c_id not in courses_map:
                    continue

                if course_id is not None and c_id != course_id:
                    continue

                c = courses_map[c_id]
                loc = s.location or (getattr(sched, "location", None) if sched else None)
                s_end = (
                    s.ends_at
                    if (s.ends_at and s.ends_at.tzinfo)
                    else (s.ends_at.replace(tzinfo=UTC) if s.ends_at else None)
                )

                aggregated_events.append(
                    CalendarEventDTO(
                        id=s.id,
                        event_type="class_session",
                        title=c.name,
                        course_id=c.id,
                        course_name=c.name,
                        course_code=c.course_code,
                        start_time=s_start,
                        end_time=s_end,
                        location=loc,
                        status=s.status,
                        priority=None,
                        description=None,
                    )
                )

        # -------------------------------------------------------------------
        # 2. Assignments
        # -------------------------------------------------------------------
        if "assignment" in active_types:
            assignments: list[Assignment] = []
            if hasattr(self.assignment_repo, "list_by_date_range"):
                assignments = await self.assignment_repo.list_by_date_range(
                    user_id=user_id,
                    from_date=from_dt,
                    to_date=to_dt,
                    course_id=course_id,
                )
            else:
                res = await self.assignment_repo.list_by_user(
                    user_id=user_id,
                    course_id=course_id,
                    limit=1000,
                )
                raw_assignments = res[0] if isinstance(res, tuple) else res
                assignments = [
                    a
                    for a in raw_assignments
                    if a.due_at is not None
                    and (from_dt <= (a.due_at if a.due_at.tzinfo else a.due_at.replace(tzinfo=UTC)) <= to_dt)
                ]

            for a in assignments:
                if not a.due_at:
                    continue
                a_due = a.due_at if a.due_at.tzinfo else a.due_at.replace(tzinfo=UTC)
                if not (from_dt <= a_due <= to_dt):
                    continue

                if a.course_id not in courses_map:
                    continue

                if course_id is not None and a.course_id != course_id:
                    continue

                c = courses_map[a.course_id]

                aggregated_events.append(
                    CalendarEventDTO(
                        id=a.id,
                        event_type="assignment",
                        title=a.title,
                        course_id=c.id,
                        course_name=c.name,
                        course_code=c.course_code,
                        start_time=a_due,
                        end_time=None,
                        location=None,
                        status=a.status,
                        priority=a.priority,
                        description=a.description,
                    )
                )

        # -------------------------------------------------------------------
        # 3. Exams
        # -------------------------------------------------------------------
        if "exam" in active_types:
            exams: list[Exam] = []
            if hasattr(self.exam_repo, "list_by_date_range"):
                exams = await self.exam_repo.list_by_date_range(
                    user_id=user_id,
                    from_date=from_dt,
                    to_date=to_dt,
                    course_id=course_id,
                )
            else:
                exam_res = await self.exam_repo.list_by_user(
                    user_id=user_id,
                    course_id=course_id,
                    limit=1000,
                )
                raw_exams: list[Exam] = exam_res[0] if isinstance(exam_res, tuple) else exam_res
                exams = [
                    e
                    for e in raw_exams
                    if from_dt <= (e.exam_at if e.exam_at.tzinfo else e.exam_at.replace(tzinfo=UTC)) <= to_dt
                ]

            for e in exams:
                e_at = e.exam_at if e.exam_at.tzinfo else e.exam_at.replace(tzinfo=UTC)
                if not (from_dt <= e_at <= to_dt):
                    continue

                if e.course_id not in courses_map:
                    continue

                if course_id is not None and e.course_id != course_id:
                    continue

                c = courses_map[e.course_id]
                e_end = (
                    e_at + timedelta(minutes=e.duration_minutes)
                    if e.duration_minutes is not None
                    else None
                )

                aggregated_events.append(
                    CalendarEventDTO(
                        id=e.id,
                        event_type="exam",
                        title=e.title,
                        course_id=c.id,
                        course_name=c.name,
                        course_code=c.course_code,
                        start_time=e_at,
                        end_time=e_end,
                        location=e.location,
                        status=None,
                        priority=None,
                        description=e.notes,
                    )
                )

        # Sort all events chronologically by start_time ASC (stable with title tie-breaker)
        aggregated_events.sort(key=lambda evt: (evt.start_time, evt.title))

        return CalendarAggregatedDTO(
            from_date=from_dt,
            to_date=to_dt,
            total_events=len(aggregated_events),
            events=aggregated_events,
        )


def get_calendar_service(
    course_repo: Annotated[CourseRepository, Depends(get_course_repo)],
    assignment_repo: Annotated[AssignmentRepository, Depends(get_assignment_repo)],
    exam_repo: Annotated[ExamRepository, Depends(get_exam_repo)],
    schedule_repo: Annotated[ScheduleRepository, Depends(get_schedule_repo)],
) -> CalendarService:
    """FastAPI dependency provider for CalendarService."""
    return CalendarService(
        course_repo=course_repo,
        assignment_repo=assignment_repo,
        exam_repo=exam_repo,
        schedule_repo=schedule_repo,
    )


__all__ = [
    "CalendarAggregatedDTO",
    "CalendarEventDTO",
    "CalendarEventType",
    "CalendarService",
    "CalendarServiceError",
    "CalendarValidationError",
    "CourseNotFoundError",
    "get_calendar_service",
]
