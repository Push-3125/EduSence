"""Exam service handling exam schedule lifecycle, dates, location, and ownership isolation (US 2.7)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends

from app.repositories.course_repo import CourseRepository, get_course_repo
from app.repositories.exam_repo import (
    Exam,
    ExamRepository,
    get_exam_repo,
)
from app.services.course_service import CourseNotFoundError

_UNSET: Any = object()


class ExamServiceError(Exception):
    """Base exception for exam service domain errors."""


class ExamNotFoundError(ExamServiceError):
    """Raised when an exam record is not found or does not belong to the user."""


class ExamValidationError(ExamServiceError):
    """Raised when exam input violates domain validation rules."""


@dataclass
class ExamDTO:
    """Decoupled Data Transfer Object representing a course exam."""

    id: uuid.UUID
    course_id: uuid.UUID
    title: str
    exam_at: datetime
    duration_minutes: int | None
    location: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class ExamService:
    """Service encapsulating exam schedule business rules and strict student ownership isolation."""

    def __init__(
        self,
        exam_repo: ExamRepository,
        course_repo: CourseRepository,
    ) -> None:
        self.exam_repo = exam_repo
        self.course_repo = course_repo

    @staticmethod
    def _to_dto(model: Exam) -> ExamDTO:
        """Convert an Exam ORM instance to decoupled ExamDTO."""
        return ExamDTO(
            id=model.id,
            course_id=model.course_id,
            title=model.title,
            exam_at=model.exam_at,
            duration_minutes=model.duration_minutes,
            location=model.location,
            notes=model.notes,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def create_exam(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID,
        title: str,
        exam_at: datetime,
        duration_minutes: int | None = None,
        location: str | None = None,
        notes: str | None = None,
    ) -> ExamDTO:
        """Create a new exam under an authenticated user's course.

        Args:
            user_id: Owner user UUID.
            course_id: Parent course UUID (must belong to user_id).
            title: Exam title.
            exam_at: Exam date and time with timezone.
            duration_minutes: Optional exam duration in minutes (must be > 0).
            location: Optional exam location (max 255 chars).
            notes: Optional notes.

        Returns:
            ExamDTO representing the created exam.

        Raises:
            CourseNotFoundError: If course does not exist or does not belong to user_id.
            ExamValidationError: If inputs fail domain validations.
        """
        course = await self.course_repo.get_by_id_and_user(course_id=course_id, user_id=user_id)
        if not course:
            raise CourseNotFoundError(f"Course '{course_id}' was not found.")

        clean_title = title.strip() if title else ""
        if not clean_title:
            raise ExamValidationError("Exam title cannot be empty or whitespace.")
        if len(clean_title) > 255:
            raise ExamValidationError("Exam title cannot exceed 255 characters.")

        if duration_minutes is not None and duration_minutes <= 0:
            raise ExamValidationError("Duration must be a positive integer (> 0 minutes).")

        clean_location = location.strip() if location else None
        if clean_location is not None and len(clean_location) > 255:
            raise ExamValidationError("Exam location cannot exceed 255 characters.")

        clean_notes = notes.strip() if notes else None

        exam = await self.exam_repo.create(
            course_id=course_id,
            title=clean_title,
            exam_at=exam_at,
            duration_minutes=duration_minutes,
            location=clean_location,
            notes=clean_notes,
        )
        return self._to_dto(exam)

    async def get_exam(
        self,
        exam_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ExamDTO:
        """Retrieve a specific exam enforcing student ownership.

        Args:
            exam_id: Exam UUID.
            user_id: Authenticated user UUID.

        Returns:
            ExamDTO.

        Raises:
            ExamNotFoundError: If exam is not found or not owned by user.
        """
        exam = await self.exam_repo.get_by_id_and_user(
            exam_id=exam_id,
            user_id=user_id,
        )
        if not exam:
            raise ExamNotFoundError(f"Exam '{exam_id}' was not found.")
        return self._to_dto(exam)

    async def list_exams(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        upcoming_only: bool = False,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[ExamDTO], int]:
        """List exams belonging to the user with optional filtering and pagination.

        Args:
            user_id: Authenticated user UUID.
            course_id: Optional course filter.
            upcoming_only: Whether to filter for upcoming exams only.
            skip: Pagination offset.
            limit: Pagination limit.

        Returns:
            Tuple of (list of ExamDTO items, total count).

        Raises:
            ExamValidationError: If pagination parameters are invalid.
        """
        if skip < 0:
            raise ExamValidationError("Pagination skip must be non-negative.")
        if limit <= 0 or limit > 100:
            raise ExamValidationError("Pagination limit must be between 1 and 100.")

        exams, total = await self.exam_repo.list_by_user(
            user_id=user_id,
            course_id=course_id,
            upcoming_only=upcoming_only,
            skip=skip,
            limit=limit,
        )
        dtos = [self._to_dto(e) for e in exams]
        return dtos, total

    async def update_exam(
        self,
        exam_id: uuid.UUID,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = _UNSET,
        title: str | None = _UNSET,
        exam_at: datetime | None = _UNSET,
        duration_minutes: int | None = _UNSET,
        location: str | None = _UNSET,
        notes: str | None = _UNSET,
    ) -> ExamDTO:
        """Update an existing exam with partial changes, validating student ownership.

        Args:
            exam_id: Exam UUID to update.
            user_id: Authenticated user UUID.
            course_id: Optional new parent course UUID.
            title: Optional new exam title.
            exam_at: Optional new exam datetime.
            duration_minutes: Optional new duration.
            location: Optional new location.
            notes: Optional new notes.

        Returns:
            Updated ExamDTO.

        Raises:
            ExamNotFoundError: If exam is not found or not owned by user.
            CourseNotFoundError: If reassigned course is not owned by user.
            ExamValidationError: If input validation fails.
        """
        exam = await self.exam_repo.get_by_id_and_user(exam_id=exam_id, user_id=user_id)
        if not exam:
            raise ExamNotFoundError(f"Exam '{exam_id}' was not found.")

        updates: dict[str, Any] = {}

        if course_id is not _UNSET:
            if course_id is None:
                raise ExamValidationError("Course ID cannot be null.")
            target_course = await self.course_repo.get_by_id_and_user(
                course_id=course_id, user_id=user_id
            )
            if not target_course:
                raise CourseNotFoundError(f"Target course '{course_id}' was not found.")
            updates["course_id"] = course_id

        if title is not _UNSET:
            if title is None:
                raise ExamValidationError("Exam title cannot be null.")
            clean_title = title.strip()
            if not clean_title:
                raise ExamValidationError("Exam title cannot be empty or whitespace.")
            if len(clean_title) > 255:
                raise ExamValidationError("Exam title cannot exceed 255 characters.")
            updates["title"] = clean_title

        if exam_at is not _UNSET:
            if exam_at is None:
                raise ExamValidationError("Exam schedule date/time cannot be null.")
            updates["exam_at"] = exam_at

        if duration_minutes is not _UNSET:
            if duration_minutes is not None and duration_minutes <= 0:
                raise ExamValidationError("Duration must be a positive integer (> 0 minutes).")
            updates["duration_minutes"] = duration_minutes

        if location is not _UNSET:
            clean_location = location.strip() if location else None
            if clean_location is not None and len(clean_location) > 255:
                raise ExamValidationError("Exam location cannot exceed 255 characters.")
            updates["location"] = clean_location

        if notes is not _UNSET:
            clean_notes = notes.strip() if notes else None
            updates["notes"] = clean_notes

        if updates:
            exam = await self.exam_repo.update(exam, **updates)

        return self._to_dto(exam)

    async def delete_exam(
        self,
        exam_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Permanently delete an exam owned by the user.

        Args:
            exam_id: Target exam UUID.
            user_id: Authenticated user UUID.

        Raises:
            ExamNotFoundError: If exam is not found or not owned by user.
        """
        exam = await self.exam_repo.get_by_id_and_user(exam_id=exam_id, user_id=user_id)
        if not exam:
            raise ExamNotFoundError(f"Exam '{exam_id}' was not found.")
        await self.exam_repo.delete(exam)


def get_exam_service(
    exam_repo: Annotated[ExamRepository, Depends(get_exam_repo)],
    course_repo: Annotated[CourseRepository, Depends(get_course_repo)],
) -> ExamService:
    """FastAPI dependency provider for ExamService."""
    return ExamService(exam_repo=exam_repo, course_repo=course_repo)


__all__ = [
    "_UNSET",
    "CourseNotFoundError",
    "ExamDTO",
    "ExamNotFoundError",
    "ExamService",
    "ExamServiceError",
    "ExamValidationError",
    "get_exam_service",
]
