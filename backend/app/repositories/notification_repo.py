"""Notification repository and SQLAlchemy model for in-app and email notifications."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
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


class Notification(Base):
    """Notification model representing notifications table."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    notification_type: Mapped[str] = mapped_column(String(30), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), default="in_app", nullable=False)
    target_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    deduplication_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship("User", lazy="select")

    __table_args__ = (
        UniqueConstraint(
            "user_id", "channel", "deduplication_key",
            name="uq_notifications_dedup",
        ),
        CheckConstraint(
            "category IN ('assignments', 'exams', 'progress', 'study_plans', 'ai')",
            name="ck_notifications_category",
        ),
        CheckConstraint(
            "channel IN ('in_app', 'email')",
            name="ck_notifications_channel",
        ),
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'cancelled')",
            name="ck_notifications_status",
        ),
        CheckConstraint(
            "NOT is_read OR read_at IS NOT NULL",
            name="ck_notifications_read_state",
        ),
        Index("idx_notifications_user_read_created", "user_id", "is_read", "created_at"),
    )


class NotificationRepository:
    """Repository handling database operations for Notification model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: uuid.UUID,
        notification_type: str,
        category: str,
        title: str,
        content: str,
        channel: str = "in_app",
        deduplication_key: str | None = None,
        target_url: str | None = None,
        scheduled_at: datetime | None = None,
        sent_at: datetime | None = None,
        status: str = "pending",
    ) -> Notification:
        """Create and persist a new notification record."""
        notification = Notification(
            user_id=user_id,
            notification_type=notification_type,
            category=category,
            title=title.strip(),
            content=content.strip(),
            channel=channel,
            deduplication_key=deduplication_key,
            target_url=target_url.strip() if target_url else None,
            scheduled_at=scheduled_at,
            sent_at=sent_at,
            status=status,
            is_read=False,
            read_at=None,
        )
        self.session.add(notification)
        await self.session.flush()
        return notification

    async def exists_by_dedup_key(
        self,
        user_id: uuid.UUID,
        channel: str,
        deduplication_key: str,
    ) -> bool:
        """Check whether a notification with the deduplication key already exists for user and channel."""
        stmt = (
            select(func.count(Notification.id))
            .where(
                Notification.user_id == user_id,
                Notification.channel == channel,
                Notification.deduplication_key == deduplication_key,
            )
        )
        result = await self.session.execute(stmt)
        return (result.scalar() or 0) > 0

    async def get_by_dedup_key(
        self,
        user_id: uuid.UUID,
        channel: str,
        deduplication_key: str,
    ) -> Notification | None:
        """Retrieve an existing notification by deduplication key."""
        stmt = (
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.channel == channel,
                Notification.deduplication_key == deduplication_key,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        is_read: bool | None = None,
        channel: str | None = None,
        category: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[Notification], int]:
        """Fetch paginated notifications for a given user."""
        filters = [Notification.user_id == user_id]
        if is_read is not None:
            filters.append(Notification.is_read == is_read)
        if channel is not None:
            filters.append(Notification.channel == channel)
        if category is not None:
            filters.append(Notification.category == category)

        count_stmt = select(func.count(Notification.id)).where(*filters)
        stmt = (
            select(Notification)
            .where(*filters)
            .order_by(Notification.created_at.desc())
            .offset(skip)
            .limit(limit)
        )

        total_res = await self.session.execute(count_stmt)
        total = total_res.scalar() or 0

        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def mark_as_read(
        self,
        notification_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Notification | None:
        """Mark a notification as read with ownership verification."""
        stmt = (
            select(Notification)
            .where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
        )
        result = await self.session.execute(stmt)
        notification = result.scalar_one_or_none()
        if not notification:
            return None

        if not notification.is_read:
            notification.is_read = True
            notification.read_at = datetime.now(UTC)
            notification.updated_at = datetime.now(UTC)
            await self.session.flush()

        return notification


def get_notification_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> NotificationRepository:
    """FastAPI dependency provider for NotificationRepository."""
    return NotificationRepository(session)


__all__ = [
    "Notification",
    "NotificationRepository",
    "get_notification_repo",
]
