"""Assignment repository and SQLAlchemy model for student assignments."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
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


class Assignment(Base):
    """Assignment model representing assignments table."""

    __tablename__ = "assignments"

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
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    priority: Mapped[str] = mapped_column(String(10), default="medium", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    course: Mapped["Course"] = relationship("Course", lazy="select")

    __table_args__ = (
        CheckConstraint(
            "priority IN ('low', 'medium', 'high')",
            name="ck_assignments_priority",
        ),
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed')",
            name="ck_assignments_status",
        ),
        Index("idx_assignments_course_due_at", "course_id", "due_at"),
        Index("idx_assignments_status_due_at", "status", "due_at"),
    )


class AssignmentRepository:
    """Repository handling database operations for Assignment model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        course_id: uuid.UUID,
        title: str,
        description: str | None = None,
        due_at: datetime | None = None,
        priority: str = "medium",
        status: str = "pending",
        completed_at: datetime | None = None,
    ) -> Assignment:
        """Create and persist a new assignment record.

        Args:
            course_id: Foreign key of parent course.
            title: Assignment title.
            description: Optional details/instructions.
            due_at: Optional due date and time.
            priority: Priority level ('low', 'medium', 'high').
            status: Status ('pending', 'in_progress', 'completed').
            completed_at: Optional completion timestamp.

        Returns:
            The created Assignment instance.
        """
        if status == "completed":
            if completed_at is None:
                completed_at = datetime.now(UTC)
        else:
            completed_at = None

        assignment = Assignment(
            course_id=course_id,
            title=title.strip(),
            description=description.strip() if description else None,
            due_at=due_at,
            priority=priority,
            status=status,
            completed_at=completed_at,
        )
        self.session.add(assignment)
        await self.session.flush()
        return assignment

    async def get_by_id(self, assignment_id: uuid.UUID) -> Assignment | None:
        """Fetch an assignment by ID without owner check."""
        stmt = select(Assignment).where(Assignment.id == assignment_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_and_user(
        self,
        assignment_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Assignment | None:
        """Fetch an assignment by ID enforcing student ownership via Course relationship.

        Args:
            assignment_id: Target assignment UUID.
            user_id: Authenticated user UUID.

        Returns:
            Assignment if found and owned by user_id, else None.
        """
        stmt = (
            select(Assignment)
            .join(Course, Assignment.course_id == Course.id)
            .where(
                Assignment.id == assignment_id,
                Course.user_id == user_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        status: str | None = None,
        priority: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Assignment], int]:
        """Fetch paginated assignments owned by user with optional filters.

        Args:
            user_id: Authenticated user UUID.
            course_id: Optional course filter.
            status: Optional status filter.
            priority: Optional priority filter.
            skip: Pagination offset.
            limit: Pagination limit.

        Returns:
            Tuple of (list of Assignment records, total count).
        """
        filters = [Course.user_id == user_id]
        if course_id is not None:
            filters.append(Assignment.course_id == course_id)
        if status is not None:
            filters.append(Assignment.status == status)
        if priority is not None:
            filters.append(Assignment.priority == priority)

        count_stmt = (
            select(func.count(Assignment.id))
            .join(Course, Assignment.course_id == Course.id)
            .where(*filters)
        )
        stmt = (
            select(Assignment)
            .join(Course, Assignment.course_id == Course.id)
            .where(*filters)
            .order_by(
                Assignment.due_at.asc().nullslast(),
                Assignment.created_at.desc(),
            )
            .offset(skip)
            .limit(limit)
        )

        total_res = await self.session.execute(count_stmt)
        total = total_res.scalar() or 0

        result = await self.session.execute(stmt)
        assignments = list(result.scalars().all())
        return assignments, total

    async def update(self, assignment: Assignment, **kwargs: Any) -> Assignment:
        """Update fields on an assignment instance and flush changes.

        If status is updated to 'completed', auto-sets completed_at to now(UTC) if not provided.
        If status changes from 'completed' to other, clears completed_at to None.

        Args:
            assignment: Existing Assignment instance.
            **kwargs: Field values to update.

        Returns:
            Updated Assignment instance.
        """
        allowed_fields = {
            "title",
            "description",
            "due_at",
            "priority",
            "status",
            "completed_at",
            "course_id",
        }

        if "status" in kwargs:
            new_status = kwargs["status"]
            if new_status == "completed":
                if "completed_at" not in kwargs or kwargs["completed_at"] is None:
                    kwargs["completed_at"] = datetime.now(UTC)
            else:
                kwargs["completed_at"] = None

        for key, value in kwargs.items():
            if key in allowed_fields:
                setattr(assignment, key, value)

        assignment.updated_at = datetime.now(UTC)
        await self.session.flush()
        return assignment

    async def delete(self, assignment: Assignment) -> None:
        """Delete an assignment record from the database.

        Args:
            assignment: Target Assignment instance to remove.
        """
        await self.session.delete(assignment)
        await self.session.flush()

    async def list_by_date_range(
        self,
        user_id: uuid.UUID,
        from_date: datetime,
        to_date: datetime,
        course_id: uuid.UUID | None = None,
    ) -> list[Assignment]:
        """Fetch all assignments owned by user with due_at in [from_date, to_date].

        Args:
            user_id: Authenticated user UUID.
            from_date: Start datetime bound.
            to_date: End datetime bound.
            course_id: Optional course UUID filter.

        Returns:
            List of matching Assignment records ordered chronologically by due_at.
        """
        from_dt = from_date if from_date.tzinfo else from_date.replace(tzinfo=UTC)
        to_dt = to_date if to_date.tzinfo else to_date.replace(tzinfo=UTC)
        filters = [
            Course.user_id == user_id,
            Assignment.due_at.is_not(None),
            Assignment.due_at >= from_dt,
            Assignment.due_at <= to_dt,
        ]
        if course_id is not None:
            filters.append(Assignment.course_id == course_id)

        stmt = (
            select(Assignment)
            .join(Course, Assignment.course_id == Course.id)
            .where(*filters)
            .order_by(Assignment.due_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


def get_assignment_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AssignmentRepository:
    """FastAPI dependency provider for AssignmentRepository with active db session."""
    return AssignmentRepository(session)


__all__ = [
    "Assignment",
    "AssignmentRepository",
    "get_assignment_repo",
]
