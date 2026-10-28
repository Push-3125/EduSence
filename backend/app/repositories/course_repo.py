"""Course repository and SQLAlchemy model for courses catalog."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, get_db
from app.repositories.user_repo import User


class Course(Base):
    """Course catalog model representing courses table."""

    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    course_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    semester: Mapped[str | None] = mapped_column(String(50), nullable=True)
    academic_year: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship("User", lazy="select")

    __table_args__ = (
        UniqueConstraint(
            "user_id", "course_code", "semester", "academic_year",
            name="uq_courses_user_code_semester_year",
        ),
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_courses_status",
        ),
    )


class CourseRepository:
    """Repository handling database operations for Course model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: uuid.UUID,
        name: str,
        course_code: str | None = None,
        description: str | None = None,
        instructor: str | None = None,
        semester: str | None = None,
        academic_year: str | None = None,
        status: str = "active",
    ) -> Course:
        """Create and persist a new course record for the authenticated user.

        Args:
            user_id: Owner user UUID.
            name: Course name/title.
            course_code: Optional course code (e.g. CS101).
            description: Optional syllabus or notes.
            instructor: Optional instructor or professor name.
            semester: Optional semester term.
            academic_year: Optional academic year.
            status: Course status ('active' or 'archived').

        Returns:
            The created Course instance.
        """
        course = Course(
            user_id=user_id,
            name=name.strip(),
            course_code=course_code.strip() if course_code else None,
            description=description.strip() if description else None,
            instructor=instructor.strip() if instructor else None,
            semester=semester.strip() if semester else None,
            academic_year=academic_year.strip() if academic_year else None,
            status=status,
        )
        self.session.add(course)
        await self.session.flush()
        return course

    async def get_by_id_and_user(
        self,
        course_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Course | None:
        """Fetch a course by primary key and verify strict ownership.

        Args:
            course_id: Target course UUID.
            user_id: Authenticated user UUID.

        Returns:
            Course if found and owned by user_id, else None.
        """
        stmt = select(Course).where(
            Course.id == course_id,
            Course.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_code_semester_year(
        self,
        user_id: uuid.UUID,
        course_code: str | None,
        semester: str | None,
        academic_year: str | None,
    ) -> Course | None:
        """Lookup an existing course matching the unique constraint tuple for the user.

        Args:
            user_id: Owner user UUID.
            course_code: Course code string or None.
            semester: Semester string or None.
            academic_year: Academic year string or None.

        Returns:
            Matching Course if found, else None.
        """
        if not course_code:
            return None

        stmt = select(Course).where(
            Course.user_id == user_id,
            func.lower(Course.course_code) == course_code.strip().lower(),
        )

        if semester is not None:
            stmt = stmt.where(Course.semester == semester.strip())
        else:
            stmt = stmt.where(Course.semester.is_(None))

        if academic_year is not None:
            stmt = stmt.where(Course.academic_year == academic_year.strip())
        else:
            stmt = stmt.where(Course.academic_year.is_(None))

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        status: str | None = None,
        semester: str | None = None,
        academic_year: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Course], int]:
        """Fetch paginated courses owned by the specified user with optional filters.

        Args:
            user_id: Authenticated user UUID.
            status: Optional status filter ('active' or 'archived').
            semester: Optional semester filter.
            academic_year: Optional academic year filter.
            skip: Offset count for pagination.
            limit: Limit count for pagination.

        Returns:
            Tuple of (list of Course records, total count).
        """
        stmt = select(Course).where(Course.user_id == user_id)
        count_stmt = select(func.count(Course.id)).where(Course.user_id == user_id)

        if status:
            stmt = stmt.where(Course.status == status)
            count_stmt = count_stmt.where(Course.status == status)

        if semester:
            stmt = stmt.where(Course.semester == semester.strip())
            count_stmt = count_stmt.where(Course.semester == semester.strip())

        if academic_year:
            stmt = stmt.where(Course.academic_year == academic_year.strip())
            count_stmt = count_stmt.where(Course.academic_year == academic_year.strip())

        stmt = stmt.order_by(Course.created_at.desc()).offset(skip).limit(limit)

        total_res = await self.session.execute(count_stmt)
        total = total_res.scalar() or 0

        result = await self.session.execute(stmt)
        courses = list(result.scalars().all())
        return courses, total

    async def update(self, course: Course, **kwargs: Any) -> Course:
        """Update fields on a course instance and flush changes.

        Args:
            course: Existing Course instance.
            **kwargs: Field values to update.

        Returns:
            Updated Course instance.
        """
        allowed_fields = {
            "name",
            "course_code",
            "description",
            "instructor",
            "semester",
            "academic_year",
            "status",
        }
        for key, value in kwargs.items():
            if key in allowed_fields:
                setattr(course, key, value)

        course.updated_at = datetime.now(UTC)
        await self.session.flush()
        return course

    async def delete(self, course: Course) -> None:
        """Delete a course record from the database.

        Args:
            course: Target Course instance to remove.
        """
        await self.session.delete(course)
        await self.session.flush()


def get_course_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> CourseRepository:
    """FastAPI dependency provider for CourseRepository with an active database session."""
    return CourseRepository(session)


__all__ = [
    "Course",
    "CourseRepository",
    "get_course_repo",
]
