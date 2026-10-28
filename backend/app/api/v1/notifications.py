"""Notifications REST API router for in-app alerts and notifications (US 2.20, Invariant 12)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from app.core.security import get_current_active_user
from app.services.auth_service import UserDTO
from app.services.notification_service import (
    NotificationDTO,
    NotificationNotFoundError,
    NotificationService,
    NotificationValidationError,
    get_notification_service,
)

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


# ---------------------------------------------------------------------------
# Presentation Schemas
# ---------------------------------------------------------------------------


class NotificationData(BaseModel):
    """Data payload representing a single notification."""

    id: uuid.UUID
    user_id: uuid.UUID
    notification_type: str
    category: str
    title: str
    content: str
    channel: str
    target_url: str | None = None
    is_read: bool
    created_at: datetime


class NotificationResponse(BaseModel):
    """Standardized API single notification response envelope."""

    success: bool = True
    data: NotificationData
    message: str = "Notification operation successful."


class NotificationListData(BaseModel):
    """Data payload for paginated notification listing."""

    items: list[NotificationData]
    total: int
    skip: int
    limit: int


class NotificationListResponse(BaseModel):
    """Standardized API list response envelope for notifications."""

    success: bool = True
    data: NotificationListData
    message: str = "Notifications retrieved successfully."


class UnreadCountData(BaseModel):
    """Data payload representing count of unread notifications."""

    unread_count: int = Field(..., ge=0, description="Total count of unread notifications")


class UnreadCountResponse(BaseModel):
    """Standardized API response envelope for unread notification count badge."""

    success: bool = True
    data: UnreadCountData
    message: str = "Unread notifications count retrieved successfully."


class MarkAllReadData(BaseModel):
    """Data payload for bulk mark-all-read action."""

    updated_count: int = Field(..., ge=0, description="Count of notifications marked as read")


class MarkAllReadResponse(BaseModel):
    """Standardized API response envelope for bulk mark-all-as-read action."""

    success: bool = True
    data: MarkAllReadData
    message: str = "All notifications marked as read."


class NotificationActionResponse(BaseModel):
    """Standardized API response envelope for simple notification mutations."""

    success: bool = True
    message: str


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _to_notification_data(dto: NotificationDTO) -> NotificationData:
    """Map domain NotificationDTO to presentation NotificationData schema."""
    return NotificationData(
        id=dto.id,
        user_id=dto.user_id,
        notification_type=dto.notification_type,
        category=dto.category,
        title=dto.title,
        content=dto.content,
        channel=dto.channel,
        target_url=dto.target_url,
        is_read=dto.is_read,
        created_at=dto.created_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=NotificationListResponse, status_code=status.HTTP_200_OK)
async def list_notifications(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    notification_service: Annotated[NotificationService, Depends(get_notification_service)],
    is_read: Annotated[
        bool | None,
        Query(description="Filter by read/unread status"),
    ] = None,
    category: Annotated[
        str | None,
        Query(
            pattern="^(assignments|exams|progress|study_plans|ai)$",
            description="Filter by notification category ('assignments', 'exams', 'progress', 'study_plans', 'ai')",
        ),
    ] = None,
    skip: Annotated[
        int,
        Query(ge=0, description="Offset count for pagination"),
    ] = 0,
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Maximum items per page"),
    ] = 50,
) -> NotificationListResponse:
    """List paginated notifications for the authenticated student with optional filters."""
    try:
        dtos, total = await notification_service.list_notifications(
            user_id=current_user.id,
            is_read=is_read,
            category=category,
            skip=skip,
            limit=limit,
        )
    except NotificationValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return NotificationListResponse(
        success=True,
        data=NotificationListData(
            items=[_to_notification_data(dto) for dto in dtos],
            total=total,
            skip=skip,
            limit=limit,
        ),
        message="Notifications retrieved successfully.",
    )


@router.get("/unread-count", response_model=UnreadCountResponse, status_code=status.HTTP_200_OK)
async def get_unread_count(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    notification_service: Annotated[NotificationService, Depends(get_notification_service)],
) -> UnreadCountResponse:
    """Retrieve unread notification count badge for the authenticated student."""
    dto = await notification_service.get_unread_count(user_id=current_user.id)
    return UnreadCountResponse(
        success=True,
        data=UnreadCountData(unread_count=dto.unread_count),
        message="Unread notifications count retrieved successfully.",
    )


@router.post("/mark-all-read", response_model=MarkAllReadResponse, status_code=status.HTTP_200_OK)
async def mark_all_notifications_as_read(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    notification_service: Annotated[NotificationService, Depends(get_notification_service)],
) -> MarkAllReadResponse:
    """Mark all unread notifications as read for the authenticated student."""
    updated_count = await notification_service.mark_all_read(user_id=current_user.id)
    return MarkAllReadResponse(
        success=True,
        data=MarkAllReadData(updated_count=updated_count),
        message="All notifications marked as read.",
    )


@router.patch("/{id}/read", response_model=NotificationResponse, status_code=status.HTTP_200_OK)
async def mark_notification_as_read(
    id: Annotated[uuid.UUID, Path(description="Notification UUID")],
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    notification_service: Annotated[NotificationService, Depends(get_notification_service)],
) -> NotificationResponse:
    """Mark a single notification as read, enforcing strict tenant isolation."""
    try:
        dto = await notification_service.mark_read(
            user_id=current_user.id,
            notification_id=id,
        )
    except NotificationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found.",
        ) from exc

    return NotificationResponse(
        success=True,
        data=_to_notification_data(dto),
        message="Notification marked as read.",
    )


@router.delete("/{id}", response_model=NotificationActionResponse, status_code=status.HTTP_200_OK)
async def delete_notification(
    id: Annotated[uuid.UUID, Path(description="Notification UUID")],
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    notification_service: Annotated[NotificationService, Depends(get_notification_service)],
) -> NotificationActionResponse:
    """Delete a notification owned by the authenticated student."""
    try:
        await notification_service.delete_notification(
            user_id=current_user.id,
            notification_id=id,
        )
    except NotificationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found.",
        ) from exc

    return NotificationActionResponse(
        success=True,
        message="Notification deleted successfully.",
    )
