"""Subscription lifecycle and atomic plan transition service (TASK-M6-02)."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends

from app.repositories.payment_repo import (
    PaymentRepository,
    get_payment_repo,
)
from app.repositories.user_repo import UserRepository, get_user_repo


class SubscriptionServiceError(Exception):
    """Base exception for subscription lifecycle operations."""


class SubscriptionNotFoundError(SubscriptionServiceError):
    """Raised when a specific subscription record is not found."""


class NoActiveSubscriptionError(SubscriptionServiceError):
    """Raised when an active subscription is expected but not found."""


@dataclass(frozen=True)
class SubscriptionDetailDTO:
    """Decoupled Data Transfer Object for subscription plan details."""

    id: uuid.UUID | None
    plan_code: str
    plan_name: str
    status: str
    starts_at: datetime | None
    ends_at: datetime | None
    is_active: bool
    days_remaining: int = 0


class SubscriptionService:
    """Business logic for subscription lifecycle, plan status, cancellation, and atomic downgrade."""

    def __init__(
        self,
        payment_repo: PaymentRepository,
        user_repo: UserRepository,
    ) -> None:
        self.payment_repo = payment_repo
        self.user_repo = user_repo

    async def get_current_subscription(
        self, user_id: uuid.UUID
    ) -> SubscriptionDetailDTO:
        """Fetch user's current active subscription or fallback to Free plan details.

        - If an active subscription exists and ends_at > now: returns Pro details with days_remaining.
        - If an active subscription exists but has expired (ends_at <= now): atomically downgrades to Free.
        - If no active subscription: returns default Free plan details (is_active=False, days_remaining=0).
        """
        active_sub = await self.payment_repo.get_active_subscription(user_id=user_id)
        now = datetime.now(UTC)

        if active_sub:
            ends_at = active_sub.ends_at
            if ends_at is not None and ends_at.tzinfo is None:
                ends_at = ends_at.replace(tzinfo=UTC)

            if ends_at is None or ends_at > now:
                days_remaining = 0
                if ends_at:
                    diff_seconds = (ends_at - now).total_seconds()
                    days_remaining = max(1, math.ceil(diff_seconds / 86400.0))

                plan_code = active_sub.plan.code if active_sub.plan else "pro"
                plan_name = active_sub.plan.name if active_sub.plan else "Pro"

                return SubscriptionDetailDTO(
                    id=active_sub.id,
                    plan_code=plan_code,
                    plan_name=plan_name,
                    status=active_sub.status,
                    starts_at=active_sub.starts_at,
                    ends_at=active_sub.ends_at,
                    is_active=True,
                    days_remaining=days_remaining,
                )
            else:
                # Active subscription has passed its expiration timestamp -> downgrade atomically
                await self.downgrade_expired_subscription(
                    user_id=user_id, subscription_id=active_sub.id
                )

        # Retrieve catalog details for default free tier
        free_plan = await self.payment_repo.get_plan_by_code("free")
        plan_name = free_plan.name if free_plan else "Free"

        return SubscriptionDetailDTO(
            id=None,
            plan_code="free",
            plan_name=plan_name,
            status="active",
            starts_at=None,
            ends_at=None,
            is_active=False,
            days_remaining=0,
        )

    async def list_subscription_history(
        self, user_id: uuid.UUID
    ) -> list[SubscriptionDetailDTO]:
        """List past subscription history for user ordered by created_at DESC (tenant isolated)."""
        subscriptions = await self.payment_repo.list_subscriptions(user_id=user_id)
        now = datetime.now(UTC)
        history: list[SubscriptionDetailDTO] = []

        for sub in subscriptions:
            ends_at = sub.ends_at
            if ends_at is not None and ends_at.tzinfo is None:
                ends_at = ends_at.replace(tzinfo=UTC)

            is_active = sub.status == "active" and (ends_at is None or ends_at > now)
            days_remaining = 0
            if is_active and ends_at:
                diff_seconds = (ends_at - now).total_seconds()
                days_remaining = max(1, math.ceil(diff_seconds / 86400.0))

            plan_code = sub.plan.code if sub.plan else "pro"
            plan_name = sub.plan.name if sub.plan else "Pro"

            history.append(
                SubscriptionDetailDTO(
                    id=sub.id,
                    plan_code=plan_code,
                    plan_name=plan_name,
                    status=sub.status,
                    starts_at=sub.starts_at,
                    ends_at=sub.ends_at,
                    is_active=is_active,
                    days_remaining=days_remaining,
                )
            )

        return history

    async def cancel_subscription(
        self, user_id: uuid.UUID
    ) -> SubscriptionDetailDTO:
        """Cancel current active subscription and atomically downgrade user to free plan.

        Raises:
            NoActiveSubscriptionError: If no active subscription exists or if it has already expired.
        """
        active_sub = await self.payment_repo.get_active_subscription(user_id=user_id)
        if not active_sub:
            raise NoActiveSubscriptionError("No active subscription found to cancel.")

        now = datetime.now(UTC)
        ends_at = active_sub.ends_at
        if ends_at is not None and ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=UTC)

        if ends_at is not None and ends_at <= now:
            await self.downgrade_expired_subscription(
                user_id=user_id, subscription_id=active_sub.id
            )
            raise NoActiveSubscriptionError("Active subscription has already expired.")

        cancelled_sub = await self.payment_repo.cancel_active_subscription(user_id=user_id)
        if not cancelled_sub:
            raise NoActiveSubscriptionError("No active subscription found to cancel.")

        # Ensure user repository record is also updated
        await self.user_repo.update_user_plan(user_id=user_id, plan_code="free")

        plan_code = cancelled_sub.plan.code if cancelled_sub.plan else "pro"
        plan_name = cancelled_sub.plan.name if cancelled_sub.plan else "Pro"

        return SubscriptionDetailDTO(
            id=cancelled_sub.id,
            plan_code=plan_code,
            plan_name=plan_name,
            status="cancelled",
            starts_at=cancelled_sub.starts_at,
            ends_at=cancelled_sub.ends_at,
            is_active=False,
            days_remaining=0,
        )

    async def downgrade_expired_subscription(
        self, user_id: uuid.UUID, subscription_id: uuid.UUID
    ) -> None:
        """Mark subscription as 'expired' and atomically downgrade user current_plan_code to 'free'.

        Raises:
            SubscriptionNotFoundError: If subscription does not exist for the user.
        """
        sub = await self.payment_repo.get_subscription_by_id(
            user_id=user_id, subscription_id=subscription_id
        )
        if not sub:
            raise SubscriptionNotFoundError(
                f"Subscription '{subscription_id}' not found for user."
            )

        await self.payment_repo.expire_subscription_and_downgrade(
            user_id=user_id, subscription_id=subscription_id
        )
        # Ensure user repo plan code is consistent
        await self.user_repo.update_user_plan(user_id=user_id, plan_code="free")


def get_subscription_service(
    payment_repo: Annotated[PaymentRepository, Depends(get_payment_repo)],
    user_repo: Annotated[UserRepository, Depends(get_user_repo)],
) -> SubscriptionService:
    """FastAPI dependency provider for SubscriptionService."""
    return SubscriptionService(payment_repo=payment_repo, user_repo=user_repo)
