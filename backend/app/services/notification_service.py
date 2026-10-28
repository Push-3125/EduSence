"""Notification service handling in-app alerts, read status, and AI quota warnings (US 2.20, Invariant 12)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends
from sqlalchemy.exc import IntegrityError

from app.repositories.notification_repo import (
    Notification,
    NotificationRepository,
    get_notification_repo,
)

VALID_CATEGORIES = frozenset({"assignments", "exams", "progress", "study_plans", "ai"})
VALID_CHANNELS = frozenset({"in_app", "email"})


# ---------------------------------------------------------------------------
# Domain Exceptions
# ---------------------------------------------------------------------------


class NotificationServiceError(Exception):
    """Base exception for notification service domain errors."""


class NotificationNotFoundError(NotificationServiceError):
    """Raised when a notification record is not found or does not belong to the user."""


class NotificationValidationError(NotificationServiceError):
    """Raised when notification parameters violate domain validation rules."""


# ---------------------------------------------------------------------------
# Data Transfer Objects
# ---------------------------------------------------------------------------


@dataclass
class NotificationDTO:
    """Decoupled Data Transfer Object representing a user notification."""

    id: uuid.UUID
    user_id: uuid.UUID
    notification_type: str
    category: str
    title: str
    content: str
    channel: str
    target_url: str | None
    is_read: bool
    created_at: datetime


@dataclass
class UnreadCountDTO:
    """Data Transfer Object representing unread notification count badge."""

    unread_count: int


# ---------------------------------------------------------------------------
# Notification Service Implementation
# ---------------------------------------------------------------------------


class NotificationService:
    """Service encapsulating notification lifecycle, ownership isolation, and quota alerts."""

    def __init__(self, notification_repo: NotificationRepository) -> None:
        self.notification_repo = notification_repo

    @staticmethod
    def _to_dto(model: Notification) -> NotificationDTO:
        """Convert an internal ORM Notification model to a decoupled NotificationDTO."""
        return NotificationDTO(
            id=model.id,
            user_id=model.user_id,
            notification_type=model.notification_type,
            category=model.category,
            title=model.title,
            content=model.content,
            channel=model.channel,
            target_url=model.target_url,
            is_read=model.is_read,
            created_at=model.created_at or datetime.now(UTC),
        )

    async def list_notifications(
        self,
        user_id: uuid.UUID,
        is_read: bool | None = None,
        category: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[NotificationDTO], int]:
        """Fetch paginated notifications strictly scoped to user_id (Invariant 6)."""
        if category is not None and category not in VALID_CATEGORIES:
            raise NotificationValidationError(
                f"Invalid category '{category}'. Must be one of: {sorted(VALID_CATEGORIES)}"
            )

        models, total = await self.notification_repo.list_by_user(
            user_id=user_id,
            is_read=is_read,
            category=category,
            channel=None,
            skip=skip,
            limit=limit,
        )
        return [self._to_dto(m) for m in models], total

    async def get_unread_count(self, user_id: uuid.UUID) -> UnreadCountDTO:
        """Get the count of unread notifications for the student badge."""
        count = await self.notification_repo.count_unread(user_id=user_id)
        return UnreadCountDTO(unread_count=count)

    async def mark_read(
        self,
        user_id: uuid.UUID,
        notification_id: uuid.UUID,
    ) -> NotificationDTO:
        """Mark a single notification as read, enforcing user ownership."""
        notification = await self.notification_repo.mark_as_read(
            user_id=user_id,
            notification_id=notification_id,
        )
        if not notification:
            raise NotificationNotFoundError(
                f"Notification {notification_id} not found."
            )
        return self._to_dto(notification)

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        """Mark all unread notifications for user_id as read in bulk."""
        return await self.notification_repo.mark_all_as_read(user_id=user_id)

    async def delete_notification(
        self,
        user_id: uuid.UUID,
        notification_id: uuid.UUID,
    ) -> None:
        """Delete a notification owned by user_id."""
        deleted = await self.notification_repo.delete_notification(
            user_id=user_id,
            notification_id=notification_id,
        )
        if not deleted:
            raise NotificationNotFoundError(
                f"Notification {notification_id} not found."
            )

    async def create_in_app_notification(
        self,
        user_id: uuid.UUID,
        notification_type: str,
        category: str,
        title: str,
        content: str,
        target_url: str | None = None,
        deduplication_key: str | None = None,
    ) -> NotificationDTO:
        """Create and persist an in-app notification with domain validation."""
        if category not in VALID_CATEGORIES:
            raise NotificationValidationError(
                f"Invalid category '{category}'. Must be one of: {sorted(VALID_CATEGORIES)}"
            )

        stripped_title = title.strip()
        if not stripped_title:
            raise NotificationValidationError("Notification title cannot be empty.")

        stripped_content = content.strip()
        if not stripped_content:
            raise NotificationValidationError("Notification content cannot be empty.")

        # Check deduplication if key provided
        if deduplication_key:
            existing = await self.notification_repo.get_by_dedup_key(
                user_id=user_id,
                channel="in_app",
                deduplication_key=deduplication_key,
            )
            if existing:
                return self._to_dto(existing)

        now = datetime.now(UTC)
        notification = await self.notification_repo.create(
            user_id=user_id,
            notification_type=notification_type.strip(),
            category=category,
            title=stripped_title,
            content=stripped_content,
            channel="in_app",
            deduplication_key=deduplication_key,
            target_url=target_url.strip() if target_url else None,
            scheduled_at=None,
            sent_at=now,
            status="sent",
        )
        return self._to_dto(notification)

    async def notify_ai_quota_warning(
        self,
        user_id: uuid.UUID,
        used: int,
        limit: int,
    ) -> NotificationDTO | None:
        """Emit warning alert when AI quota usage hits or exceeds 80% (Invariant 12, DECISION-PROP-04).

        Uses daily deduplication key `quota_warning:{user_id}:{date_utc}` to avoid duplicate spam.
        Returns NotificationDTO if a warning alert was created, or None if below threshold / already emitted today.
        """
        if limit <= 0 or used < 0:
            return None

        # Warning threshold is 80% usage
        threshold = limit * 0.8
        if used < threshold:
            return None

        date_utc = datetime.now(UTC).strftime("%Y-%m-%d")
        deduplication_key = f"quota_warning:{user_id}:{date_utc}"

        # Check if already emitted today
        if await self.notification_repo.exists_by_dedup_key(
            user_id=user_id,
            channel="in_app",
            deduplication_key=deduplication_key,
        ):
            return None

        now = datetime.now(UTC)
        try:
            notification = await self.notification_repo.create(
                user_id=user_id,
                notification_type="quota_warning",
                category="ai",
                title="AI Quota Warning",
                content=(
                    f"You have reached 80% of your daily AI request limit "
                    f"({used}/{limit} requests used). Quota resets at 00:00 UTC."
                ),
                channel="in_app",
                deduplication_key=deduplication_key,
                target_url="/settings",
                scheduled_at=None,
                sent_at=now,
                status="sent",
            )
            return self._to_dto(notification)
        except IntegrityError:
            # Concurrent race condition caught by PostgreSQL uq_notifications_dedup constraint
            return None


def get_notification_service(
    repo: Annotated[NotificationRepository, Depends(get_notification_repo)],
) -> NotificationService:
    """FastAPI dependency provider for NotificationService."""
    return NotificationService(repo)


__all__ = [
    "NotificationDTO",
    "NotificationNotFoundError",
    "NotificationService",
    "NotificationServiceError",
    "NotificationValidationError",
    "UnreadCountDTO",
    "get_notification_service",
]
