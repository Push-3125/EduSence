"""Course service handling course lifecycle, validations, and ownership isolation (US 2.5)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends

from app.repositories.course_repo import Course, CourseRepository, get_course_repo

_UNSET: Any = object()


class CourseServiceError(Exception):
    """Base exception for course service domain errors."""


class CourseNotFoundError(CourseServiceError):
    """Raised when a course record is not found or does not belong to the user."""


class CourseConflictError(CourseServiceError):
    """Raised when a course operation violates uniqueness constraints."""


class CourseValidationError(CourseServiceError):
    """Raised when course input violates domain validation rules."""


@dataclass
class CourseDTO:
    """Decoupled Data Transfer Object representing an academic course."""

    id: uuid.UUID
    user_id: uuid.UUID
    course_code: str | None
    name: str
    description: str | None
    instructor: str | None
    semester: str | None
    academic_year: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class CourseService:
    """Service encapsulating course management business rules and ownership isolation."""

    def __init__(self, course_repo: CourseRepository) -> None:
        self.course_repo = course_repo

    async def create_course(
        self,
        user_id: uuid.UUID,
        name: str,
        course_code: str | None = None,
        description: str | None = None,
        instructor: str | None = None,
        semester: str | None = None,
        academic_year: str | None = None,
        status: str = "active",
    ) -> CourseDTO:
        """Create a new course after validating inputs and uniqueness constraints.

        Args:
            user_id: Owner user UUID.
            name: Course name/title.
            course_code: Optional course code.
            description: Optional course description.
            instructor: Optional instructor name.
            semester: Optional semester.
            academic_year: Optional academic year.
            status: Course status ('active' or 'archived').

        Returns:
            CourseDTO representing the created course.

        Raises:
            CourseValidationError: If name or status is invalid.
            CourseConflictError: If a duplicate course code/semester/year exists for this user.
        """
        clean_name = name.strip() if name else ""
        if not clean_name:
            raise CourseValidationError("Course name cannot be empty or whitespace.")
        if len(clean_name) > 255:
            raise CourseValidationError("Course name cannot exceed 255 characters.")

        if status not in ("active", "archived"):
            raise CourseValidationError("Status must be either 'active' or 'archived'.")

        clean_code = course_code.strip() if course_code else None
        clean_desc = description.strip() if description else None
        clean_inst = instructor.strip() if instructor else None
        clean_sem = semester.strip() if semester else None
        clean_year = academic_year.strip() if academic_year else None

        if clean_code:
            existing = await self.course_repo.get_by_code_semester_year(
                user_id=user_id,
                course_code=clean_code,
                semester=clean_sem,
                academic_year=clean_year,
            )
            if existing:
                sem_str = clean_sem or "unspecified"
                year_str = clean_year or "unspecified"
                raise CourseConflictError(
                    f"A course with code '{clean_code}' in semester '{sem_str}' "
                    f"and academic year '{year_str}' already exists."
                )

        course = await self.course_repo.create(
            user_id=user_id,
            name=clean_name,
            course_code=clean_code,
            description=clean_desc,
            instructor=clean_inst,
            semester=clean_sem,
            academic_year=clean_year,
            status=status,
        )
        return self._to_dto(course)

    async def get_course(
        self,
        course_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> CourseDTO:
        """Fetch a course by ID ensuring tenant ownership isolation.

        Args:
            course_id: Target course UUID.
            user_id: Authenticated user UUID.

        Returns:
            CourseDTO representing the course.

        Raises:
            CourseNotFoundError: If course does not exist or belongs to another user.
        """
        course = await self.course_repo.get_by_id_and_user(course_id, user_id)
        if not course:
            raise CourseNotFoundError(f"Course {course_id} not found.")
        return self._to_dto(course)

    async def list_courses(
        self,
        user_id: uuid.UUID,
        status: str | None = None,
        semester: str | None = None,
        academic_year: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[CourseDTO], int]:
        """List paginated courses owned strictly by the authenticated user.

        Args:
            user_id: Authenticated user UUID.
            status: Optional filter by status ('active' or 'archived').
            semester: Optional semester filter.
            academic_year: Optional academic year filter.
            skip: Pagination offset.
            limit: Pagination limit.

        Returns:
            Tuple of (list of CourseDTO items, total count).

        Raises:
            CourseValidationError: If status or pagination arguments are invalid.
        """
        if status and status not in ("active", "archived"):
            raise CourseValidationError("Status filter must be either 'active' or 'archived'.")
        if skip < 0:
            raise CourseValidationError("Skip cannot be negative.")
        if limit < 1 or limit > 100:
            raise CourseValidationError("Limit must be between 1 and 100.")

        courses, total = await self.course_repo.list_by_user(
            user_id=user_id,
            status=status,
            semester=semester.strip() if semester else None,
            academic_year=academic_year.strip() if academic_year else None,
            skip=skip,
            limit=limit,
        )
        return [self._to_dto(c) for c in courses], total

    async def update_course(
        self,
        course_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str | None | Any = _UNSET,
        course_code: str | None | Any = _UNSET,
        description: str | None | Any = _UNSET,
        instructor: str | None | Any = _UNSET,
        semester: str | None | Any = _UNSET,
        academic_year: str | None | Any = _UNSET,
        status: str | None | Any = _UNSET,
        **kwargs: Any,
    ) -> CourseDTO:
        """Update an existing course with partial or full attributes.

        Args:
            course_id: Target course UUID.
            user_id: Authenticated user UUID.
            name: Optional new course name.
            course_code: Optional new course code.
            description: Optional new description.
            instructor: Optional new instructor.
            semester: Optional new semester.
            academic_year: Optional new academic year.
            status: Optional new status ('active' or 'archived').
            **kwargs: Extra parameters for flexibility.

        Returns:
            Updated CourseDTO.

        Raises:
            CourseNotFoundError: If course is not found or owned by user.
            CourseValidationError: If field formats or constraints are invalid.
            CourseConflictError: If new code/semester/year conflicts with another course.
        """
        course = await self.course_repo.get_by_id_and_user(course_id, user_id)
        if not course:
            raise CourseNotFoundError(f"Course {course_id} not found.")

        update_fields: dict[str, Any] = {}

        if name is not _UNSET:
            if name is None or not str(name).strip():
                raise CourseValidationError("Course name cannot be empty or whitespace.")
            clean_name = str(name).strip()
            if len(clean_name) > 255:
                raise CourseValidationError("Course name cannot exceed 255 characters.")
            update_fields["name"] = clean_name

        if status is not _UNSET:
            if status not in ("active", "archived"):
                raise CourseValidationError("Status must be either 'active' or 'archived'.")
            update_fields["status"] = status

        if description is not _UNSET:
            update_fields["description"] = (
                description.strip() if isinstance(description, str) and description.strip() else None
            )

        if instructor is not _UNSET:
            update_fields["instructor"] = (
                instructor.strip() if isinstance(instructor, str) and instructor.strip() else None
            )

        if course_code is not _UNSET:
            update_fields["course_code"] = (
                course_code.strip() if isinstance(course_code, str) and course_code.strip() else None
            )

        if semester is not _UNSET:
            update_fields["semester"] = (
                semester.strip() if isinstance(semester, str) and semester.strip() else None
            )

        if academic_year is not _UNSET:
            update_fields["academic_year"] = (
                academic_year.strip() if isinstance(academic_year, str) and academic_year.strip() else None
            )

        # Check unique constraint if any component of the unique tuple is updated
        target_code = update_fields.get("course_code", course.course_code)
        target_sem = update_fields.get("semester", course.semester)
        target_year = update_fields.get("academic_year", course.academic_year)

        if target_code:
            existing = await self.course_repo.get_by_code_semester_year(
                user_id=user_id,
                course_code=target_code,
                semester=target_sem,
                academic_year=target_year,
            )
            if existing and existing.id != course.id:
                sem_str = target_sem or "unspecified"
                year_str = target_year or "unspecified"
                raise CourseConflictError(
                    f"A course with code '{target_code}' in semester '{sem_str}' "
                    f"and academic year '{year_str}' already exists."
                )

        if not update_fields:
            return self._to_dto(course)

        updated_course = await self.course_repo.update(course, **update_fields)
        return self._to_dto(updated_course)

    async def archive_course(
        self,
        course_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> CourseDTO:
        """Mark a course as archived.

        Args:
            course_id: Target course UUID.
            user_id: Authenticated user UUID.

        Returns:
            Updated CourseDTO with status='archived'.

        Raises:
            CourseNotFoundError: If course is not found or owned by user.
        """
        course = await self.course_repo.get_by_id_and_user(course_id, user_id)
        if not course:
            raise CourseNotFoundError(f"Course {course_id} not found.")

        updated_course = await self.course_repo.update(course, status="archived")
        return self._to_dto(updated_course)

    async def delete_course(
        self,
        course_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Permanently delete a course owned by the authenticated user.

        Args:
            course_id: Target course UUID.
            user_id: Authenticated user UUID.

        Raises:
            CourseNotFoundError: If course is not found or owned by user.
        """
        course = await self.course_repo.get_by_id_and_user(course_id, user_id)
        if not course:
            raise CourseNotFoundError(f"Course {course_id} not found.")

        await self.course_repo.delete(course)

    @staticmethod
    def _to_dto(course: Course) -> CourseDTO:
        """Map Course ORM model to domain CourseDTO."""
        return CourseDTO(
            id=course.id,
            user_id=course.user_id,
            course_code=course.course_code,
            name=course.name,
            description=course.description,
            instructor=course.instructor,
            semester=course.semester,
            academic_year=course.academic_year,
            status=course.status,
            created_at=course.created_at,
            updated_at=course.updated_at,
        )


def get_course_service(
    course_repo: Annotated[CourseRepository, Depends(get_course_repo)],
) -> CourseService:
    """FastAPI dependency provider for CourseService."""
    return CourseService(course_repo)


__all__ = [
    "CourseConflictError",
    "CourseDTO",
    "CourseNotFoundError",
    "CourseService",
    "CourseServiceError",
    "CourseValidationError",
    "get_course_service",
]
