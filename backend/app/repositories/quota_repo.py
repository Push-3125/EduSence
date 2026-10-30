"""Quota policy repository and SQLAlchemy model for plan-level AI limits (US 2.23 / TASK-M6-05).

Adheres strictly to:
- Invariant 2: Thin router -> Service -> Repository layering.
- Table schema from initial baseline: quota_policies.
- Constraints:
  * ck_quota_policies_requests: request_limit_per_day >= 0
  * ck_quota_policies_tokens: token_limit_per_day IS NULL OR token_limit_per_day >= 0
  * ck_quota_policies_warning: warning_threshold_percent BETWEEN 1 AND 100
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, get_db
from app.repositories.user_repo import SubscriptionPlan, User


class QuotaPolicyNotFoundError(Exception):
    """Raised when a quota policy for a requested subscription plan code does not exist."""


class QuotaPolicy(Base):
    """Plan-level quota defaults editable by Administrator without application restart."""

    __tablename__ = "quota_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscription_plans.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    request_limit_per_day: Mapped[int] = mapped_column(Integer, nullable=False)
    token_limit_per_day: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    warning_threshold_percent: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=80
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    plan: Mapped[SubscriptionPlan] = relationship("SubscriptionPlan", lazy="joined")
    updater: Mapped[User | None] = relationship(
        "User", foreign_keys=[updated_by], lazy="joined"
    )

    __table_args__ = (
        CheckConstraint(
            "request_limit_per_day >= 0",
            name="ck_quota_policies_requests",
        ),
        CheckConstraint(
            "token_limit_per_day IS NULL OR token_limit_per_day >= 0",
            name="ck_quota_policies_tokens",
        ),
        CheckConstraint(
            "warning_threshold_percent BETWEEN 1 AND 100",
            name="ck_quota_policies_warning",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<QuotaPolicy id={self.id} plan_id={self.plan_id} "
            f"requests/day={self.request_limit_per_day} tokens/day={self.token_limit_per_day} "
            f"warning={self.warning_threshold_percent}%>"
        )


class QuotaPolicyRepository:
    """Repository handling database operations for plan-level AI quota policies."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all_policies(self) -> list[QuotaPolicy]:
        """Return list of all quota policies joined with plan and updater."""
        stmt = (
            select(QuotaPolicy)
            .join(QuotaPolicy.plan)
            .order_by(SubscriptionPlan.code.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_plan_code(self, plan_code: str) -> QuotaPolicy | None:
        """Find quota policy by subscription plan code (case-insensitive)."""
        norm_code = plan_code.strip().lower()
        stmt = (
            select(QuotaPolicy)
            .join(QuotaPolicy.plan)
            .where(func.lower(SubscriptionPlan.code) == norm_code)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def update_policy(
        self,
        plan_code: str,
        request_limit_per_day: int,
        token_limit_per_day: int | None,
        warning_threshold_percent: int,
        updated_by: uuid.UUID,
    ) -> QuotaPolicy:
        """Update or create quota policy limits for a plan and flush changes.

        Args:
            plan_code: Subscription plan code (e.g. 'free', 'pro').
            request_limit_per_day: Daily request ceiling (>= 0).
            token_limit_per_day: Daily token ceiling (>= 0 or None).
            warning_threshold_percent: Usage percent triggering warning (1..100).
            updated_by: UUID of the administrator performing the update.

        Returns:
            The updated QuotaPolicy instance.

        Raises:
            ValueError: If constraints are violated.
            QuotaPolicyNotFoundError: If plan does not exist.
        """
        if request_limit_per_day < 0:
            raise ValueError("request_limit_per_day must be non-negative (>= 0)")
        if token_limit_per_day is not None and token_limit_per_day < 0:
            raise ValueError("token_limit_per_day must be non-negative (>= 0) or None")
        if not (1 <= warning_threshold_percent <= 100):
            raise ValueError("warning_threshold_percent must be between 1 and 100")

        norm_code = plan_code.strip().lower()
        policy = await self.get_by_plan_code(norm_code)

        if not policy:
            plan_stmt = select(SubscriptionPlan).where(
                func.lower(SubscriptionPlan.code) == norm_code
            )
            plan_result = await self.session.execute(plan_stmt)
            plan = plan_result.scalars().first()
            if not plan:
                raise QuotaPolicyNotFoundError(
                    f"Subscription plan '{plan_code}' not found."
                )

            policy = QuotaPolicy(
                plan_id=plan.id,
                request_limit_per_day=request_limit_per_day,
                token_limit_per_day=token_limit_per_day,
                warning_threshold_percent=warning_threshold_percent,
                updated_by=updated_by,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            self.session.add(policy)
        else:
            policy.request_limit_per_day = request_limit_per_day
            policy.token_limit_per_day = token_limit_per_day
            policy.warning_threshold_percent = warning_threshold_percent
            policy.updated_by = updated_by
            policy.updated_at = datetime.now(UTC)

        await self.session.flush()
        # Re-fetch policy with joined relationships to ensure plan and updater are loaded
        refetched = await self.get_by_plan_code(norm_code)
        return refetched or policy


def get_quota_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> QuotaPolicyRepository:
    """FastAPI dependency provider for QuotaPolicyRepository."""
    return QuotaPolicyRepository(session)


__all__ = [
    "QuotaPolicy",
    "QuotaPolicyNotFoundError",
    "QuotaPolicyRepository",
    "get_quota_repo",
]
