"""Course Academic Progress repository and SQLAlchemy model (TASK-M5-07 / US 2.17, 2.18).

Adheres strictly to:
- Invariant 2: Layered architecture (Routers -> Services -> Repositories).
- Invariant 6: Strict tenant isolation (all operations scoped to authenticated user_id).
- PostgreSQL Schema: course_progress with exact check and unique constraints.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload

from app.core.database import Base, get_db
from app.repositories.course_repo import Course
from app.repositories.user_repo import User


class CourseProgress(Base):
    """CourseProgress model representing course_progress table."""

    __tablename__ = "course_progress"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assignments_total: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    assignments_completed: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    study_items_total: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    study_items_completed: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    progress_percent: Mapped[float] = mapped_column(
        Numeric(5, 2), default=0.0, server_default="0", nullable=False
    )
    risk_status: Mapped[str] = mapped_column(
        String(20), default="on_track", server_default="on_track", nullable=False
    )
    last_calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        onupdate=func.now(),
        nullable=False,
    )

    course: Mapped[Course] = relationship("Course", lazy="select")
    user: Mapped[User] = relationship("User", lazy="select")

    __table_args__ = (
        UniqueConstraint("user_id", "course_id", name="uq_course_progress_user_course"),
        CheckConstraint(
            "assignments_total >= 0 AND assignments_completed >= 0 "
            "AND study_items_total >= 0 AND study_items_completed >= 0 "
            "AND assignments_completed <= assignments_total "
            "AND study_items_completed <= study_items_total",
            name="ck_course_progress_counts",
        ),
        CheckConstraint(
            "progress_percent BETWEEN 0 AND 100",
            name="ck_course_progress_percent",
        ),
        CheckConstraint(
            "risk_status IN ('on_track', 'at_risk', 'overdue')",
            name="ck_course_progress_risk",
        ),
    )


class ProgressRepository:
    """Repository managing database persistence and queries for CourseProgress."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_course_progress(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID,
        assignments_total: int,
        assignments_completed: int,
        study_items_total: int,
        study_items_completed: int,
        progress_percent: float,
        risk_status: str,
    ) -> CourseProgress:
        """Upsert a course_progress record using PostgreSQL ON CONFLICT DO UPDATE.

        Args:
            user_id: Owner user UUID.
            course_id: Target course UUID.
            assignments_total: Total non-archived assignments count.
            assignments_completed: Completed assignments count.
            study_items_total: Total active study plan items count.
            study_items_completed: Completed study plan items count.
            progress_percent: Blended completion percentage [0.0, 100.0].
            risk_status: Risk classification ('on_track', 'at_risk', 'overdue').

        Returns:
            The upserted CourseProgress model instance.
        """
        now = datetime.now(UTC)
        insert_stmt = pg_insert(CourseProgress).values(
            user_id=user_id,
            course_id=course_id,
            assignments_total=assignments_total,
            assignments_completed=assignments_completed,
            study_items_total=study_items_total,
            study_items_completed=study_items_completed,
            progress_percent=progress_percent,
            risk_status=risk_status,
            last_calculated_at=now,
            created_at=now,
            updated_at=now,
        )
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=["user_id", "course_id"],
            set_={
                "assignments_total": insert_stmt.excluded.assignments_total,
                "assignments_completed": insert_stmt.excluded.assignments_completed,
                "study_items_total": insert_stmt.excluded.study_items_total,
                "study_items_completed": insert_stmt.excluded.study_items_completed,
                "progress_percent": insert_stmt.excluded.progress_percent,
                "risk_status": insert_stmt.excluded.risk_status,
                "last_calculated_at": insert_stmt.excluded.last_calculated_at,
                "updated_at": func.now(),
            },
        ).returning(CourseProgress)

        result = await self.session.execute(upsert_stmt)
        await self.session.flush()
        return result.scalar_one()

    async def get_progress(
        self, user_id: uuid.UUID, course_id: uuid.UUID
    ) -> CourseProgress | None:
        """Fetch progress for a single course owned by user."""
        stmt = (
            select(CourseProgress)
            .options(selectinload(CourseProgress.course))
            .where(
                CourseProgress.user_id == user_id,
                CourseProgress.course_id == course_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_progress(self, user_id: uuid.UUID) -> list[CourseProgress]:
        """Fetch all course progress records owned by user."""
        stmt = (
            select(CourseProgress)
            .options(selectinload(CourseProgress.course))
            .where(CourseProgress.user_id == user_id)
            .order_by(CourseProgress.updated_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


def get_progress_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ProgressRepository:
    """FastAPI dependency provider for ProgressRepository with active db session."""
    return ProgressRepository(session=session)


__all__ = [
    "CourseProgress",
    "ProgressRepository",
    "get_progress_repo",
]
