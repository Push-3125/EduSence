"""Exam repository and SQLAlchemy model for student exams schedule tracking."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, get_db
from app.repositories.course_repo import Course
from app.repositories.user_repo import User


class Exam(Base):
    """Exam model representing exams table."""

    __tablename__ = "exams"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    exam_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    course: Mapped["Course"] = relationship("Course", lazy="select")

    __table_args__ = (
        Index("idx_exams_course_exam_at", "course_id", "exam_at"),
        Index("idx_exams_exam_at", "exam_at"),
    )


class ExamRepository:
    """Repository handling database operations for Exam model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        course_id: uuid.UUID,
        title: str,
        exam_at: datetime,
        duration_minutes: int | None = None,
        location: str | None = None,
        notes: str | None = None,
    ) -> Exam:
        """Create and persist a new exam record.

        Args:
            course_id: Foreign key of parent course.
            title: Exam title.
            exam_at: Scheduled exam date and time with timezone.
            duration_minutes: Optional duration in minutes.
            location: Optional exam location (classroom, hall, or URL).
            notes: Optional notes or instructions.

        Returns:
            The created Exam instance.
        """
        exam = Exam(
            course_id=course_id,
            title=title.strip(),
            exam_at=exam_at,
            duration_minutes=duration_minutes,
            location=location.strip() if location else None,
            notes=notes.strip() if notes else None,
        )
        self.session.add(exam)
        await self.session.flush()
        return exam

    async def get_by_id(self, exam_id: uuid.UUID) -> Exam | None:
        """Fetch an exam by ID without owner check."""
        stmt = select(Exam).where(Exam.id == exam_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_and_user(
        self,
        exam_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Exam | None:
        """Fetch an exam by ID enforcing student ownership via Course relationship.

        Args:
            exam_id: Target exam UUID.
            user_id: Authenticated user UUID.

        Returns:
            Exam if found and owned by user_id, else None.
        """
        stmt = (
            select(Exam)
            .join(Course, Exam.course_id == Course.id)
            .where(
                Exam.id == exam_id,
                Course.user_id == user_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        upcoming_only: bool = False,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Exam], int]:
        """Fetch paginated exams owned by user with optional course and upcoming filters.

        Args:
            user_id: Authenticated user UUID.
            course_id: Optional course UUID filter.
            upcoming_only: If True, only include exams with exam_at >= current UTC time.
            skip: Pagination offset.
            limit: Pagination limit.

        Returns:
            Tuple of (list of Exam records, total count).
        """
        filters = [Course.user_id == user_id]
        if course_id is not None:
            filters.append(Exam.course_id == course_id)
        if upcoming_only:
            filters.append(Exam.exam_at >= datetime.now(UTC))

        count_stmt = (
            select(func.count(Exam.id))
            .join(Course, Exam.course_id == Course.id)
            .where(*filters)
        )
        stmt = (
            select(Exam)
            .join(Course, Exam.course_id == Course.id)
            .where(*filters)
            .order_by(
                Exam.exam_at.asc(),
                Exam.created_at.desc(),
            )
            .offset(skip)
            .limit(limit)
        )

        total_res = await self.session.execute(count_stmt)
        total = total_res.scalar() or 0

        result = await self.session.execute(stmt)
        exams = list(result.scalars().all())
        return exams, total

    async def update(self, exam: Exam, **kwargs: Any) -> Exam:
        """Update fields on an exam instance and flush changes.

        Args:
            exam: Existing Exam instance.
            **kwargs: Field values to update.

        Returns:
            Updated Exam instance.
        """
        allowed_fields = {
            "course_id",
            "title",
            "exam_at",
            "duration_minutes",
            "location",
            "notes",
        }
        for key, value in kwargs.items():
            if key in allowed_fields:
                setattr(exam, key, value)

        exam.updated_at = datetime.now(UTC)
        await self.session.flush()
        return exam

    async def delete(self, exam: Exam) -> None:
        """Delete an exam record from the database.

        Args:
            exam: Target Exam instance to remove.
        """
        await self.session.delete(exam)
        await self.session.flush()

    async def list_by_date_range(
        self,
        user_id: uuid.UUID,
        from_date: datetime,
        to_date: datetime,
        course_id: uuid.UUID | None = None,
    ) -> list[Exam]:
        """Fetch all exams owned by user with exam_at in [from_date, to_date].

        Args:
            user_id: Authenticated user UUID.
            from_date: Start datetime bound.
            to_date: End datetime bound.
            course_id: Optional course UUID filter.

        Returns:
            List of matching Exam records ordered chronologically by exam_at.
        """
        from_dt = from_date if from_date.tzinfo else from_date.replace(tzinfo=UTC)
        to_dt = to_date if to_date.tzinfo else to_date.replace(tzinfo=UTC)
        filters = [
            Course.user_id == user_id,
            Exam.exam_at >= from_dt,
            Exam.exam_at <= to_dt,
        ]
        if course_id is not None:
            filters.append(Exam.course_id == course_id)

        stmt = (
            select(Exam)
            .join(Course, Exam.course_id == Course.id)
            .where(*filters)
            .order_by(Exam.exam_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_exams_between(
        self,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[tuple[Exam, Course, str]]:
        """Fetch exams scheduled between start_dt and end_dt across all active users.

        Args:
            start_dt: Lower bound of exam scheduled time.
            end_dt: Upper bound of exam scheduled time.

        Returns:
            List of tuples: (Exam, Course, user_email).
        """
        start = start_dt if start_dt.tzinfo else start_dt.replace(tzinfo=UTC)
        end = end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=UTC)

        stmt = (
            select(Exam, Course, User.email)
            .join(Course, Exam.course_id == Course.id)
            .join(User, Course.user_id == User.id)
            .where(
                Exam.exam_at >= start,
                Exam.exam_at <= end,
                User.is_active.is_(True),
            )
            .order_by(Exam.exam_at.asc())
        )
        result = await self.session.execute(stmt)
        return [(row[0], row[1], row[2]) for row in result.all()]


def get_exam_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ExamRepository:
    """FastAPI dependency provider for ExamRepository with active db session."""
    return ExamRepository(session)


__all__ = [
    "Exam",
    "ExamRepository",
    "get_exam_repo",
]
