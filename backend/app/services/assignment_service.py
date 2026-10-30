"""Assignment service handling assignment lifecycle, due dates, priority, and ownership isolation (US 2.6)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends

from app.repositories.assignment_repo import (
    Assignment,
    AssignmentRepository,
    get_assignment_repo,
)
from app.repositories.course_repo import CourseRepository, get_course_repo
from app.services.course_service import CourseNotFoundError

_UNSET: Any = object()


class AssignmentServiceError(Exception):
    """Base exception for assignment service domain errors."""


class AssignmentNotFoundError(AssignmentServiceError):
    """Raised when an assignment record is not found or does not belong to the user."""


class AssignmentValidationError(AssignmentServiceError):
    """Raised when assignment input violates domain validation rules."""


@dataclass
class AssignmentDTO:
    """Decoupled Data Transfer Object representing a course assignment."""

    id: uuid.UUID
    course_id: uuid.UUID
    title: str
    description: str | None
    due_at: datetime | None
    priority: str
    status: str
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AssignmentService:
    """Service encapsulating assignment management business rules and strict ownership isolation."""

    def __init__(
        self,
        assignment_repo: AssignmentRepository,
        course_repo: CourseRepository,
    ) -> None:
        self.assignment_repo = assignment_repo
        self.course_repo = course_repo

    @staticmethod
    def _to_dto(model: Assignment) -> AssignmentDTO:
        """Convert an Assignment ORM instance to decoupled AssignmentDTO."""
        return AssignmentDTO(
            id=model.id,
            course_id=model.course_id,
            title=model.title,
            description=model.description,
            due_at=model.due_at,
            priority=model.priority,
            status=model.status,
            completed_at=model.completed_at,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def create_assignment(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID,
        title: str,
        description: str | None = None,
        due_at: datetime | None = None,
        priority: str = "medium",
        status: str = "pending",
    ) -> AssignmentDTO:
        """Create a new assignment under an authenticated user's course.

        Args:
            user_id: Owner user UUID.
            course_id: Parent course UUID (must belong to user_id).
            title: Assignment title.
            description: Optional assignment description.
            due_at: Optional due date and time.
            priority: Priority ('low', 'medium', 'high').
            status: Status ('pending', 'in_progress', 'completed').

        Returns:
            AssignmentDTO representing the created assignment.

        Raises:
            CourseNotFoundError: If course does not exist or does not belong to user_id.
            AssignmentValidationError: If inputs fail domain validations.
        """
        course = await self.course_repo.get_by_id_and_user(course_id=course_id, user_id=user_id)
        if not course:
            raise CourseNotFoundError(f"Course '{course_id}' was not found.")

        clean_title = title.strip() if title else ""
        if not clean_title:
            raise AssignmentValidationError("Assignment title cannot be empty or whitespace.")
        if len(clean_title) > 255:
            raise AssignmentValidationError("Assignment title cannot exceed 255 characters.")

        clean_priority = priority.strip().lower() if priority else "medium"
        if clean_priority not in ("low", "medium", "high"):
            raise AssignmentValidationError("Priority must be 'low', 'medium', or 'high'.")

        clean_status = status.strip().lower() if status else "pending"
        if clean_status not in ("pending", "in_progress", "completed"):
            raise AssignmentValidationError("Status must be 'pending', 'in_progress', or 'completed'.")

        clean_desc = description.strip() if description else None

        assignment = await self.assignment_repo.create(
            course_id=course_id,
            title=clean_title,
            description=clean_desc,
            due_at=due_at,
            priority=clean_priority,
            status=clean_status,
        )
        return self._to_dto(assignment)

    async def get_assignment(
        self,
        assignment_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> AssignmentDTO:
        """Retrieve a specific assignment enforcing user ownership.

        Args:
            assignment_id: Assignment UUID.
            user_id: Authenticated user UUID.

        Returns:
            AssignmentDTO.

        Raises:
            AssignmentNotFoundError: If assignment is not found or not owned by user.
        """
        assignment = await self.assignment_repo.get_by_id_and_user(
            assignment_id=assignment_id,
            user_id=user_id,
        )
        if not assignment:
            raise AssignmentNotFoundError(f"Assignment '{assignment_id}' was not found.")
        return self._to_dto(assignment)

    async def list_assignments(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        status: str | None = None,
        priority: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[AssignmentDTO], int]:
        """List assignments belonging to the user with optional filtering and pagination.

        Args:
            user_id: Authenticated user UUID.
            course_id: Optional course filter.
            status: Optional status filter.
            priority: Optional priority filter.
            skip: Pagination offset.
            limit: Pagination limit.

        Returns:
            Tuple of (list of AssignmentDTO items, total count).

        Raises:
            AssignmentValidationError: If filters or pagination parameters are invalid.
        """
        if skip < 0:
            raise AssignmentValidationError("Pagination skip must be non-negative.")
        if limit <= 0 or limit > 100:
            raise AssignmentValidationError("Pagination limit must be between 1 and 100.")

        clean_status = status.strip().lower() if status else None
        if clean_status is not None and clean_status not in ("pending", "in_progress", "completed"):
            raise AssignmentValidationError("Status filter must be 'pending', 'in_progress', or 'completed'.")

        clean_priority = priority.strip().lower() if priority else None
        if clean_priority is not None and clean_priority not in ("low", "medium", "high"):
            raise AssignmentValidationError("Priority filter must be 'low', 'medium', or 'high'.")

        assignments, total = await self.assignment_repo.list_by_user(
            user_id=user_id,
            course_id=course_id,
            status=clean_status,
            priority=clean_priority,
            skip=skip,
            limit=limit,
        )
        dtos = [self._to_dto(a) for a in assignments]
        return dtos, total

    async def update_assignment(
        self,
        assignment_id: uuid.UUID,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = _UNSET,
        title: str | None = _UNSET,
        description: str | None = _UNSET,
        due_at: datetime | None = _UNSET,
        priority: str | None = _UNSET,
        status: str | None = _UNSET,
    ) -> AssignmentDTO:
        """Update an existing assignment with partial changes, validating ownership.

        Args:
            assignment_id: Target assignment UUID.
            user_id: Authenticated user UUID.
            course_id: Optional new course UUID.
            title: Optional updated title.
            description: Optional updated description.
            due_at: Optional updated due date/time.
            priority: Optional updated priority.
            status: Optional updated status.

        Returns:
            Updated AssignmentDTO.

        Raises:
            AssignmentNotFoundError: If assignment is not found or not owned by user.
            CourseNotFoundError: If reassigned course_id does not exist or belong to user.
            AssignmentValidationError: If field values violate validation rules.
        """
        assignment = await self.assignment_repo.get_by_id_and_user(
            assignment_id=assignment_id,
            user_id=user_id,
        )
        if not assignment:
            raise AssignmentNotFoundError(f"Assignment '{assignment_id}' was not found.")

        updates: dict[str, Any] = {}

        if course_id is not _UNSET:
            if course_id is None:
                raise AssignmentValidationError("Course ID cannot be null.")
            new_course = await self.course_repo.get_by_id_and_user(
                course_id=course_id,
                user_id=user_id,
            )
            if not new_course:
                raise CourseNotFoundError(f"Course '{course_id}' was not found.")
            updates["course_id"] = course_id

        if title is not _UNSET:
            if title is None or not title.strip():
                raise AssignmentValidationError("Assignment title cannot be empty or whitespace.")
            clean_title = title.strip()
            if len(clean_title) > 255:
                raise AssignmentValidationError("Assignment title cannot exceed 255 characters.")
            updates["title"] = clean_title

        if description is not _UNSET:
            updates["description"] = description.strip() if description else None

        if due_at is not _UNSET:
            updates["due_at"] = due_at

        if priority is not _UNSET:
            if priority is None:
                raise AssignmentValidationError("Priority cannot be null.")
            clean_priority = priority.strip().lower()
            if clean_priority not in ("low", "medium", "high"):
                raise AssignmentValidationError("Priority must be 'low', 'medium', or 'high'.")
            updates["priority"] = clean_priority

        if status is not _UNSET:
            if status is None:
                raise AssignmentValidationError("Status cannot be null.")
            clean_status = status.strip().lower()
            if clean_status not in ("pending", "in_progress", "completed"):
                raise AssignmentValidationError("Status must be 'pending', 'in_progress', or 'completed'.")
            updates["status"] = clean_status

        updated = await self.assignment_repo.update(assignment, **updates)
        return self._to_dto(updated)

    async def delete_assignment(
        self,
        assignment_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Permanently delete an assignment owned by the student.

        Args:
            assignment_id: Target assignment UUID.
            user_id: Authenticated user UUID.

        Raises:
            AssignmentNotFoundError: If assignment is not found or not owned by user.
        """
        assignment = await self.assignment_repo.get_by_id_and_user(
            assignment_id=assignment_id,
            user_id=user_id,
        )
        if not assignment:
            raise AssignmentNotFoundError(f"Assignment '{assignment_id}' was not found.")

        await self.assignment_repo.delete(assignment)


def get_assignment_service(
    assignment_repo: Annotated[AssignmentRepository, Depends(get_assignment_repo)],
    course_repo: Annotated[CourseRepository, Depends(get_course_repo)],
) -> AssignmentService:
    """FastAPI dependency provider for AssignmentService."""
    return AssignmentService(assignment_repo=assignment_repo, course_repo=course_repo)


__all__ = [
    "_UNSET",
    "AssignmentDTO",
    "AssignmentNotFoundError",
    "AssignmentService",
    "AssignmentServiceError",
    "AssignmentValidationError",
    "CourseNotFoundError",
    "get_assignment_service",
]
