"""Subscriptions REST API router for plan details, history, and cancellation (TASK-M6-02)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.security import get_current_active_user
from app.services.auth_service import UserDTO
from app.services.subscription_service import (
    NoActiveSubscriptionError,
    SubscriptionService,
    get_subscription_service,
)

router = APIRouter(prefix="/api/v1/subscriptions", tags=["subscriptions"])


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------


class SubscriptionData(BaseModel):
    """Payload representing subscription details and entitlement."""

    id: uuid.UUID | None = None
    plan_code: str
    plan_name: str
    status: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool
    days_remaining: int = 0


class SubscriptionResponse(BaseModel):
    """Standardized response envelope for single subscription plan."""

    success: bool = True
    data: SubscriptionData
    message: str = "Subscription retrieved successfully."


class SubscriptionHistoryResponse(BaseModel):
    """Standardized response envelope for subscription history listing."""

    success: bool = True
    data: list[SubscriptionData]
    message: str = "Subscription history retrieved successfully."


# ---------------------------------------------------------------------------
# Router Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/current",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current active plan and subscription",
)
async def get_current_subscription(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> SubscriptionResponse:
    """Retrieve student's current active plan and subscription details."""
    dto = await service.get_current_subscription(user_id=current_user.id)
    return SubscriptionResponse(
        data=SubscriptionData(
            id=dto.id,
            plan_code=dto.plan_code,
            plan_name=dto.plan_name,
            status=dto.status,
            starts_at=dto.starts_at,
            ends_at=dto.ends_at,
            is_active=dto.is_active,
            days_remaining=dto.days_remaining,
        ),
        message="Current subscription retrieved successfully.",
    )


@router.get(
    "/history",
    response_model=SubscriptionHistoryResponse,
    status_code=status.HTTP_200_OK,
    summary="List past subscription history",
)
async def list_subscription_history(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> SubscriptionHistoryResponse:
    """List subscription history for authenticated user ordered by created_at DESC."""
    history_dtos = await service.list_subscription_history(user_id=current_user.id)
    return SubscriptionHistoryResponse(
        data=[
            SubscriptionData(
                id=dto.id,
                plan_code=dto.plan_code,
                plan_name=dto.plan_name,
                status=dto.status,
                starts_at=dto.starts_at,
                ends_at=dto.ends_at,
                is_active=dto.is_active,
                days_remaining=dto.days_remaining,
            )
            for dto in history_dtos
        ],
        message="Subscription history retrieved successfully.",
    )


@router.post(
    "/cancel",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_200_OK,
    summary="Cancel active subscription",
)
async def cancel_subscription(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    service: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> SubscriptionResponse:
    """Cancel student's active subscription and downgrade current plan to 'free'."""
    try:
        dto = await service.cancel_subscription(user_id=current_user.id)
    except NoActiveSubscriptionError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e

    return SubscriptionResponse(
        data=SubscriptionData(
            id=dto.id,
            plan_code=dto.plan_code,
            plan_name=dto.plan_name,
            status=dto.status,
            starts_at=dto.starts_at,
            ends_at=dto.ends_at,
            is_active=dto.is_active,
            days_remaining=dto.days_remaining,
        ),
        message="Subscription cancelled successfully. Plan downgraded to free.",
    )
