"""Study Plan and Study Plan Item repository and SQLAlchemy models (TASK-M5-05 / US 2.16).

Adheres strictly to:
- Invariant 2: Layered architecture (Routers -> Services -> Repositories).
- Invariant 6: Strict tenant isolation (all operations scoped to authenticated user_id).
- PostgreSQL Schema: study_plans & study_plan_items with exact check constraints.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload

from app.core.database import Base, get_db
from app.repositories.course_repo import Course
from app.repositories.user_repo import User


class StudyPlan(Base):
    """StudyPlan model representing study_plans table."""

    __tablename__ = "study_plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft", nullable=False
    )
    generated_by_ai: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
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

    items: Mapped[list[StudyPlanItem]] = relationship(
        "StudyPlanItem",
        back_populates="plan",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="[StudyPlanItem.scheduled_start.asc(), StudyPlanItem.display_order.asc()]",
    )
    user: Mapped[User] = relationship("User", lazy="select")

    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_study_plans_date"),
        CheckConstraint(
            "status IN ('draft', 'active', 'completed', 'archived')",
            name="ck_study_plans_status",
        ),
        Index("idx_study_plans_user_status", "user_id", "status"),
    )


class StudyPlanItem(Base):
    """StudyPlanItem model representing study_plan_items table."""

    __tablename__ = "study_plan_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    study_plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("study_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    scheduled_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    display_order: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
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

    plan: Mapped[StudyPlan] = relationship("StudyPlan", back_populates="items")
    course: Mapped[Course | None] = relationship("Course", lazy="select")

    __table_args__ = (
        CheckConstraint(
            "scheduled_end IS NULL OR scheduled_end > scheduled_start",
            name="ck_study_plan_items_time",
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'in_progress', 'completed', 'dismissed')",
            name="ck_study_plan_items_status",
        ),
        CheckConstraint(
            "status NOT IN ('accepted', 'in_progress', 'completed') OR accepted_at IS NOT NULL",
            name="ck_study_plan_items_accepted_at",
        ),
        CheckConstraint(
            "status <> 'completed' OR completed_at IS NOT NULL",
            name="ck_study_plan_items_completed_at",
        ),
        CheckConstraint("display_order >= 0", name="ck_study_plan_items_order"),
        Index("idx_study_plan_items_plan_status_start", "study_plan_id", "status", "scheduled_start"),
    )


class StudyPlanRepository:
    """Repository managing database persistence and tenant-isolated operations for Study Plans."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_plan(
        self,
        user_id: uuid.UUID,
        name: str,
        start_date: date,
        end_date: date,
        description: str | None = None,
        generated_by_ai: bool = False,
        status: str = "draft",
    ) -> StudyPlan:
        """Create and persist a new study plan."""
        plan = StudyPlan(
            user_id=user_id,
            name=name.strip(),
            start_date=start_date,
            end_date=end_date,
            description=description.strip() if description else None,
            generated_by_ai=generated_by_ai,
            status=status,
        )
        self.session.add(plan)
        await self.session.commit()
        await self.session.refresh(plan)
        return plan

    async def get_plan(
        self, user_id: uuid.UUID, plan_id: uuid.UUID
    ) -> StudyPlan | None:
        """Fetch study plan by id and verify strict ownership by user_id."""
        stmt = (
            select(StudyPlan)
            .options(selectinload(StudyPlan.items))
            .where(StudyPlan.id == plan_id, StudyPlan.user_id == user_id)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def list_plans(
        self,
        user_id: uuid.UUID,
        skip: int = 0,
        limit: int = 50,
        status: str | None = None,
    ) -> tuple[list[StudyPlan], int]:
        """List study plans for user with pagination and optional status filter."""
        count_stmt = select(func.count(StudyPlan.id)).where(StudyPlan.user_id == user_id)
        stmt = (
            select(StudyPlan)
            .options(selectinload(StudyPlan.items))
            .where(StudyPlan.user_id == user_id)
        )
        if status:
            count_stmt = count_stmt.where(StudyPlan.status == status)
            stmt = stmt.where(StudyPlan.status == status)

        stmt = (
            stmt.order_by(StudyPlan.start_date.desc(), StudyPlan.created_at.desc())
            .offset(skip)
            .limit(limit)
        )

        total = (await self.session.execute(count_stmt)).scalar() or 0
        plans = (await self.session.execute(stmt)).scalars().all()
        return list(plans), total

    async def delete_plan(self, user_id: uuid.UUID, plan_id: uuid.UUID) -> bool:
        """Delete study plan if owned by user_id. Cascades to items."""
        plan = await self.get_plan(user_id=user_id, plan_id=plan_id)
        if plan is None:
            return False
        await self.session.delete(plan)
        await self.session.commit()
        return True

    async def add_items(
        self, plan_id: uuid.UUID, items_data: list[dict[str, Any]]
    ) -> list[StudyPlanItem]:
        """Bulk insert items into an existing study plan."""
        created_items: list[StudyPlanItem] = []
        for data in items_data:
            item = StudyPlanItem(study_plan_id=plan_id, **data)
            self.session.add(item)
            created_items.append(item)
        await self.session.commit()
        for item in created_items:
            await self.session.refresh(item)
        return created_items

    async def get_item(
        self, user_id: uuid.UUID, plan_id: uuid.UUID, item_id: uuid.UUID
    ) -> StudyPlanItem | None:
        """Fetch item verifying ownership through parent study plan."""
        stmt = (
            select(StudyPlanItem)
            .join(StudyPlan, StudyPlanItem.study_plan_id == StudyPlan.id)
            .where(
                StudyPlanItem.id == item_id,
                StudyPlanItem.study_plan_id == plan_id,
                StudyPlan.user_id == user_id,
            )
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def update_item_status(
        self,
        user_id: uuid.UUID,
        plan_id: uuid.UUID,
        item_id: uuid.UUID,
        status: str,
        accepted_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> StudyPlanItem | None:
        """Update study plan item status enforcing accepted_at and completed_at constraints."""
        item = await self.get_item(user_id=user_id, plan_id=plan_id, item_id=item_id)
        if item is None:
            return None

        now = datetime.now(UTC)
        item.status = status

        if accepted_at is not None:
            item.accepted_at = accepted_at
        elif status in ("accepted", "in_progress", "completed") and item.accepted_at is None:
            item.accepted_at = now

        if completed_at is not None:
            item.completed_at = completed_at
        elif status == "completed" and item.completed_at is None:
            item.completed_at = now

        await self.session.commit()
        await self.session.refresh(item)
        return item


def get_study_plan_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> StudyPlanRepository:
    """FastAPI dependency provider for StudyPlanRepository."""
    return StudyPlanRepository(session=session)


__all__ = [
    "StudyPlan",
    "StudyPlanItem",
    "StudyPlanRepository",
    "get_study_plan_repo",
]
