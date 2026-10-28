"""Class schedule and session repository and SQLAlchemy models (US 2.9)."""

import uuid
from datetime import UTC, date, datetime, time
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, get_db
from app.repositories.course_repo import Course


class ClassSchedule(Base):
    """Class schedule model representing recurring weekly class definitions."""

    __tablename__ = "class_schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    course: Mapped["Course"] = relationship("Course", lazy="select")
    sessions: Mapped[list["ClassSession"]] = relationship(
        "ClassSession",
        back_populates="schedule",
        cascade="all, delete-orphan",
        lazy="select",
    )

    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="ck_class_schedules_day"),
        CheckConstraint("end_time > start_time", name="ck_class_schedules_time"),
        CheckConstraint("end_date >= start_date", name="ck_class_schedules_date"),
        Index("idx_class_schedules_course_id", "course_id"),
    )


class ClassSession(Base):
    """Class session model representing individual instances generated from recurring schedules."""

    __tablename__ = "class_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("class_schedules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="scheduled", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    schedule: Mapped["ClassSchedule"] = relationship(
        "ClassSchedule", back_populates="sessions", lazy="select"
    )

    __table_args__ = (
        UniqueConstraint("schedule_id", "starts_at", name="uq_class_sessions_schedule_start"),
        CheckConstraint("ends_at > starts_at", name="ck_class_sessions_time"),
        CheckConstraint("status IN ('scheduled', 'completed', 'cancelled')", name="ck_class_sessions_status"),
        Index("idx_class_sessions_starts_at", "starts_at"),
    )


class ScheduleRepository:
    """Repository handling database operations for ClassSchedule and ClassSession."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_schedule(
        self,
        course_id: uuid.UUID,
        day_of_week: int,
        start_time: time,
        end_time: time,
        start_date: date,
        end_date: date,
        location: str | None = None,
    ) -> ClassSchedule:
        """Create and persist a new recurring weekly class schedule record.

        Args:
            course_id: Foreign key of parent course.
            day_of_week: Day index (0=Monday, 6=Sunday).
            start_time: Daily recurring start time.
            end_time: Daily recurring end time.
            start_date: Start date of term/schedule range.
            end_date: End date of term/schedule range.
            location: Optional classroom or meeting room.

        Returns:
            Created ClassSchedule instance.
        """
        schedule = ClassSchedule(
            course_id=course_id,
            day_of_week=day_of_week,
            start_time=start_time,
            end_time=end_time,
            start_date=start_date,
            end_date=end_date,
            location=location.strip() if location else None,
        )
        self.session.add(schedule)
        await self.session.flush()
        return schedule

    async def create_sessions_batch(
        self,
        sessions_data: list[dict[str, Any]],
    ) -> list[ClassSession]:
        """Bulk insert concrete class sessions generated from a schedule.

        Args:
            sessions_data: List of dicts with session fields.

        Returns:
            List of created ClassSession instances.
        """
        if not sessions_data:
            return []

        sessions = [
            ClassSession(
                id=item.get("id", uuid.uuid4()),
                schedule_id=item["schedule_id"],
                starts_at=item["starts_at"],
                ends_at=item["ends_at"],
                location=item.get("location"),
                status=item.get("status", "scheduled"),
            )
            for item in sessions_data
        ]
        self.session.add_all(sessions)
        await self.session.flush()
        return sessions

    async def get_schedule_by_id(
        self,
        schedule_id: uuid.UUID,
    ) -> ClassSchedule | None:
        """Fetch a schedule by ID without tenant isolation check."""
        stmt = select(ClassSchedule).where(ClassSchedule.id == schedule_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_schedule_by_id_and_user(
        self,
        schedule_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ClassSchedule | None:
        """Fetch a schedule by ID verifying student ownership via Course join.

        Args:
            schedule_id: Schedule UUID.
            user_id: Authenticated user UUID.

        Returns:
            ClassSchedule if found and owned by user_id, else None.
        """
        stmt = (
            select(ClassSchedule)
            .join(Course, ClassSchedule.course_id == Course.id)
            .where(
                ClassSchedule.id == schedule_id,
                Course.user_id == user_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_schedules_by_user(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[ClassSchedule], int]:
        """List schedules owned by user with optional course filter and pagination.

        Args:
            user_id: Authenticated user UUID.
            course_id: Optional course filter.
            skip: Pagination offset.
            limit: Page limit.

        Returns:
            Tuple of (list of ClassSchedule, total count).
        """
        base_query = (
            select(ClassSchedule)
            .join(Course, ClassSchedule.course_id == Course.id)
            .where(Course.user_id == user_id)
        )
        if course_id is not None:
            base_query = base_query.where(ClassSchedule.course_id == course_id)

        count_stmt = select(func.count()).select_from(base_query.subquery())
        count_res = await self.session.execute(count_stmt)
        total = count_res.scalar_one() or 0

        stmt = (
            base_query
            .order_by(
                ClassSchedule.start_date.asc(),
                ClassSchedule.day_of_week.asc(),
                ClassSchedule.start_time.asc(),
            )
            .offset(skip)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def list_sessions_by_user(
        self,
        user_id: uuid.UUID,
        schedule_id: uuid.UUID | None = None,
        from_date: datetime | date | None = None,
        to_date: datetime | date | None = None,
        status: str | None = None,
    ) -> list[ClassSession]:
        """List concrete class sessions for user, enforcing course ownership.

        Args:
            user_id: Authenticated user UUID.
            schedule_id: Optional schedule UUID filter.
            from_date: Optional filter for sessions starting on or after timestamp/date.
            to_date: Optional filter for sessions starting on or before timestamp/date.
            status: Optional filter by session status ('scheduled', 'completed', 'cancelled').

        Returns:
            List of matching ClassSession instances ordered chronologically.
        """
        stmt = (
            select(ClassSession)
            .join(ClassSchedule, ClassSession.schedule_id == ClassSchedule.id)
            .join(Course, ClassSchedule.course_id == Course.id)
            .where(Course.user_id == user_id)
        )

        if schedule_id is not None:
            stmt = stmt.where(ClassSession.schedule_id == schedule_id)

        if from_date is not None:
            if isinstance(from_date, datetime):
                from_dt = from_date if from_date.tzinfo else from_date.replace(tzinfo=UTC)
            else:
                from_dt = datetime.combine(from_date, time.min, tzinfo=UTC)
            stmt = stmt.where(ClassSession.starts_at >= from_dt)

        if to_date is not None:
            if isinstance(to_date, datetime):
                to_dt = to_date if to_date.tzinfo else to_date.replace(tzinfo=UTC)
            else:
                to_dt = datetime.combine(to_date, time.max, tzinfo=UTC)
            stmt = stmt.where(ClassSession.starts_at <= to_dt)

        if status is not None:
            stmt = stmt.where(ClassSession.status == status)

        stmt = stmt.order_by(ClassSession.starts_at.asc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_session_by_id(
        self,
        session_id: uuid.UUID,
    ) -> ClassSession | None:
        """Fetch a session by ID without tenant isolation check."""
        stmt = select(ClassSession).where(ClassSession.id == session_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_session_by_id_and_user(
        self,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ClassSession | None:
        """Fetch a session by ID enforcing student ownership via schedule and course.

        Args:
            session_id: Target session UUID.
            user_id: Authenticated user UUID.

        Returns:
            ClassSession if found and owned by user_id, else None.
        """
        stmt = (
            select(ClassSession)
            .join(ClassSchedule, ClassSession.schedule_id == ClassSchedule.id)
            .join(Course, ClassSchedule.course_id == Course.id)
            .where(
                ClassSession.id == session_id,
                Course.user_id == user_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_session(
        self,
        session: ClassSession,
        **kwargs: Any,
    ) -> ClassSession:
        """Update fields on an existing ClassSession.

        Args:
            session: Target ClassSession instance.
            **kwargs: Field-value pairs to update.

        Returns:
            Updated ClassSession instance.
        """
        allowed_fields = {"status", "location", "starts_at", "ends_at"}
        for key, value in kwargs.items():
            if key in allowed_fields and value is not None:
                setattr(session, key, value)

        session.updated_at = datetime.now(UTC)
        await self.session.flush()
        return session

    async def delete_schedule(
        self,
        schedule: ClassSchedule,
    ) -> None:
        """Delete a schedule record from the database (cascades sessions).

        Args:
            schedule: Target ClassSchedule instance to remove.
        """
        await self.session.delete(schedule)
        await self.session.flush()


def get_schedule_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ScheduleRepository:
    """FastAPI dependency provider for ScheduleRepository with an active database session."""
    return ScheduleRepository(session)


__all__ = [
    "ClassSchedule",
    "ClassSession",
    "ScheduleRepository",
    "get_schedule_repo",
]
