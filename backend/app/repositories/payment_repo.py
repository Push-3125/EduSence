"""Payment and Subscription repository for SePay QR orders, webhooks, and plan activations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    String,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload

from app.core.database import Base, get_db
from app.repositories.user_repo import SubscriptionPlan, User


class Subscription(Base):
    """User subscription lifecycle entity representing subscriptions table."""

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscription_plans.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False, index=True
    )
    starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    # Relationships
    user: Mapped[User] = relationship("User", foreign_keys=[user_id], lazy="select")
    plan: Mapped[SubscriptionPlan] = relationship(
        "SubscriptionPlan", foreign_keys=[plan_id], lazy="selectin"
    )
    transactions: Mapped[list[PaymentTransaction]] = relationship(
        "PaymentTransaction",
        back_populates="subscription",
        lazy="select",
    )


class PaymentTransaction(Base):
    """Payment transaction model for SePay payment tracking."""

    __tablename__ = "payment_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(
        String(30), default="sepay", nullable=False
    )
    sepay_transaction_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )
    reference_code: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    amount_vnd: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    currency: Mapped[str] = mapped_column(
        String(3), default="VND", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False, index=True
    )
    provider_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    webhook_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    # Relationships
    user: Mapped[User] = relationship("User", foreign_keys=[user_id], lazy="select")
    subscription: Mapped[Subscription | None] = relationship(
        "Subscription",
        back_populates="transactions",
        foreign_keys=[subscription_id],
        lazy="selectin",
    )


class PaymentRepository:
    """PostgreSQL repository handling transactions, subscriptions, and plans."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_plan_by_code(self, code: str) -> SubscriptionPlan | None:
        """Fetch subscription plan by unique code (e.g., 'free', 'pro')."""
        stmt = select(SubscriptionPlan).where(SubscriptionPlan.code == code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_payment_order(
        self,
        user_id: uuid.UUID,
        plan_id: uuid.UUID,
        amount_vnd: int,
        reference_code: str,
    ) -> tuple[PaymentTransaction, Subscription]:
        """Create a pending subscription and associated payment transaction order."""
        subscription = Subscription(
            user_id=user_id,
            plan_id=plan_id,
            status="pending",
        )
        self.session.add(subscription)
        await self.session.flush()

        transaction = PaymentTransaction(
            user_id=user_id,
            subscription_id=subscription.id,
            provider="sepay",
            reference_code=reference_code,
            amount_vnd=amount_vnd,
            currency="VND",
            status="pending",
        )
        self.session.add(transaction)
        await self.session.commit()
        await self.session.refresh(transaction)
        await self.session.refresh(subscription)
        return transaction, subscription

    async def get_transaction_by_reference(
        self, reference_code: str
    ) -> PaymentTransaction | None:
        """Find payment transaction by reference transfer code."""
        stmt = (
            select(PaymentTransaction)
            .options(selectinload(PaymentTransaction.subscription))
            .where(PaymentTransaction.reference_code == reference_code)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_transaction_by_sepay_id(
        self, sepay_transaction_id: str
    ) -> PaymentTransaction | None:
        """Find payment transaction by unique SePay transaction id."""
        stmt = (
            select(PaymentTransaction)
            .options(selectinload(PaymentTransaction.subscription))
            .where(PaymentTransaction.sepay_transaction_id == sepay_transaction_id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_transaction(
        self, user_id: uuid.UUID, transaction_id: uuid.UUID
    ) -> PaymentTransaction | None:
        """Find payment transaction by user_id and transaction_id (tenant isolated)."""
        stmt = (
            select(PaymentTransaction)
            .options(selectinload(PaymentTransaction.subscription))
            .where(
                PaymentTransaction.id == transaction_id,
                PaymentTransaction.user_id == user_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def complete_payment_and_activate_subscription(
        self,
        transaction_id: uuid.UUID,
        sepay_transaction_id: str,
        provider_payload: dict[str, Any],
        duration_days: int = 30,
    ) -> PaymentTransaction:
        """Atomically mark transaction completed, expire previous active sub, activate new sub, and update user plan."""
        stmt = (
            select(PaymentTransaction)
            .options(selectinload(PaymentTransaction.subscription))
            .where(PaymentTransaction.id == transaction_id)
            .with_for_update()
        )
        res = await self.session.execute(stmt)
        transaction = res.scalar_one_or_none()
        if not transaction:
            raise ValueError(f"Payment transaction {transaction_id} not found.")

        # Idempotent guard: if already completed, return existing
        if transaction.status == "completed":
            return transaction

        now = datetime.now(UTC)

        # 1. Update payment transaction record
        transaction.status = "completed"
        transaction.sepay_transaction_id = sepay_transaction_id
        transaction.paid_at = now
        transaction.webhook_received_at = now
        transaction.provider_payload = provider_payload

        # 2. Expire any existing active subscription for this user to preserve uq_subscriptions_one_active_per_user
        await self.session.execute(
            update(Subscription)
            .where(
                Subscription.user_id == transaction.user_id,
                Subscription.status == "active",
            )
            .values(
                status="expired",
                updated_at=now,
            )
        )

        # 3. Mark associated subscription active with starting & ending timestamps
        if transaction.subscription_id:
            await self.session.execute(
                update(Subscription)
                .where(Subscription.id == transaction.subscription_id)
                .values(
                    status="active",
                    starts_at=now,
                    ends_at=now + timedelta(days=duration_days),
                    activated_at=now,
                    updated_at=now,
                )
            )

        # 4. Update user's current_plan_code to 'pro' atomically
        await self.session.execute(
            update(User)
            .where(User.id == transaction.user_id)
            .values(
                current_plan_code="pro",
                updated_at=now,
            )
        )

        await self.session.commit()
        await self.session.refresh(transaction)
        return transaction


def get_payment_repo(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PaymentRepository:
    """FastAPI dependency provider for PaymentRepository."""
    return PaymentRepository(session=db)
