"""AI usage repository and SQLAlchemy model for token tracking and telemetry (TASK-M4-03 / Invariant 4 & 7).

Adheres strictly to:
- Invariant 4 & 7: Never log prompts, document content, or user messages in plain form.
  Only record tokens, model, feature, latency, status, and cost.
- Database Check Constraints:
  * ck_ai_usage_logs_tokens: prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0
    AND total_tokens = prompt_tokens + completion_tokens
  * ck_ai_usage_logs_cost: estimated_cost >= 0
  * ck_ai_usage_logs_latency: latency_ms IS NULL OR latency_ms >= 0
  * ck_ai_usage_logs_status: request_status IN ('success', 'failed', 'timeout', 'rejected')
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, get_db

VALID_REQUEST_STATUSES = frozenset({"success", "failed", "timeout", "rejected"})


class AIUsageLog(Base):
    """AIUsageLog model representing ai_usage_logs table for token tracking and telemetry."""

    __tablename__ = "ai_usage_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    feature: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="google")
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0.0")
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="success"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0 "
            "AND total_tokens = prompt_tokens + completion_tokens",
            name="ck_ai_usage_logs_tokens",
        ),
        CheckConstraint("estimated_cost >= 0", name="ck_ai_usage_logs_cost"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_ai_usage_logs_latency"),
        CheckConstraint(
            "request_status IN ('success', 'failed', 'timeout', 'rejected')",
            name="ck_ai_usage_logs_status",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AIUsageLog id={self.id} user_id={self.user_id} feature='{self.feature}' "
            f"model='{self.model_name}' total_tokens={self.total_tokens} status='{self.request_status}'>"
        )


class UsageSummary(BaseModel):
    """Aggregated AI usage summary for a user over a sliding time window."""

    model_config = ConfigDict(from_attributes=True)

    total_requests: int = Field(default=0, ge=0, description="Total number of AI requests recorded")
    total_prompt_tokens: int = Field(default=0, ge=0, description="Total prompt tokens consumed")
    total_completion_tokens: int = Field(
        default=0, ge=0, description="Total completion tokens consumed"
    )
    total_tokens: int = Field(default=0, ge=0, description="Total tokens consumed")
    total_cost: float = Field(default=0.0, ge=0.0, description="Total estimated cost in USD")


class AIUsageRepository:
    """Repository handling database operations for AI usage telemetry and token tracking."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_log(
        self,
        user_id: uuid.UUID,
        feature: str,
        model_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int | None = None,
        latency_ms: int | None = None,
        request_status: str = "success",
        error_message: str | None = None,
        conversation_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        estimated_cost: Decimal | float = 0.0,
        provider: str = "google",
    ) -> AIUsageLog:
        """Create and persist an AI usage telemetry log entry.

        Strictly enforces database constraints before executing SQL insert:
        - prompt_tokens >= 0
        - completion_tokens >= 0
        - total_tokens = prompt_tokens + completion_tokens (computed if None)
        - latency_ms IS NULL OR latency_ms >= 0
        - estimated_cost >= 0
        - request_status IN ('success', 'failed', 'timeout', 'rejected')

        Args:
            user_id: Student/user UUID.
            feature: AI feature name ('chat', 'rag', 'summary', 'questions', 'embedding', etc.).
            model_name: AI model name (e.g. 'gemini-2.5-flash').
            prompt_tokens: Number of prompt/input tokens (must be >= 0).
            completion_tokens: Number of completion/output tokens (must be >= 0).
            total_tokens: Total tokens consumed. If None, set to prompt_tokens + completion_tokens.
            latency_ms: Latency in milliseconds (must be >= 0 or None).
            request_status: Request outcome ('success', 'failed', 'timeout', 'rejected').
            error_message: Optional error message for non-success statuses.
            conversation_id: Optional UUID of the associated AI conversation.
            message_id: Optional UUID of the associated AI message.
            estimated_cost: Estimated cost in USD (must be >= 0).
            provider: AI provider name (defaults to 'google').

        Returns:
            The persisted AIUsageLog instance.

        Raises:
            ValueError: If any check constraint is violated.
        """
        if prompt_tokens < 0:
            raise ValueError("prompt_tokens must be non-negative (>= 0)")
        if completion_tokens < 0:
            raise ValueError("completion_tokens must be non-negative (>= 0)")

        computed_total = prompt_tokens + completion_tokens
        if total_tokens is None:
            resolved_total_tokens = computed_total
        else:
            if total_tokens < 0:
                raise ValueError("total_tokens must be non-negative (>= 0)")
            if total_tokens != computed_total:
                raise ValueError(
                    f"total_tokens ({total_tokens}) does not match prompt_tokens + completion_tokens ({computed_total})"
                )
            resolved_total_tokens = total_tokens

        if latency_ms is not None and latency_ms < 0:
            raise ValueError("latency_ms must be non-negative (>= 0) or None")

        if request_status not in VALID_REQUEST_STATUSES:
            raise ValueError(
                f"Invalid request_status '{request_status}'. Must be one of {sorted(VALID_REQUEST_STATUSES)}"
            )

        cost_decimal = (
            Decimal(str(estimated_cost))
            if not isinstance(estimated_cost, Decimal)
            else estimated_cost
        )
        if cost_decimal < Decimal(0):
            raise ValueError("estimated_cost must be non-negative (>= 0)")

        # Quantize cost to 6 decimal places to match NUMERIC(12, 6)
        cost_decimal = cost_decimal.quantize(Decimal("0.000001"))

        log_entry = AIUsageLog(
            id=uuid.uuid4(),
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            feature=feature.strip().lower() if feature else "unknown",
            provider=provider.strip().lower() if provider else "google",
            model_name=model_name.strip(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=resolved_total_tokens,
            estimated_cost=cost_decimal,
            latency_ms=latency_ms,
            request_status=request_status,
            error_message=error_message,
        )

        self.session.add(log_entry)
        await self.session.flush()
        return log_entry

    async def get_user_summary(
        self,
        user_id: uuid.UUID,
        days: int = 30,
        now: datetime | None = None,
    ) -> UsageSummary:
        """Calculate aggregated usage metrics for a user within a sliding window.

        Args:
            user_id: Student/user UUID.
            days: Sliding window in days (defaults to 30, must be > 0).
            now: Optional reference timestamp (defaults to current UTC time).

        Returns:
            UsageSummary DTO containing aggregated tokens, requests, and cost.

        Raises:
            ValueError: If days <= 0.
        """
        if days <= 0:
            raise ValueError("days must be greater than 0")

        current_time = now or datetime.now(UTC)
        start_time = current_time - timedelta(days=days)

        stmt = (
            select(
                func.count(AIUsageLog.id).label("total_requests"),
                func.coalesce(func.sum(AIUsageLog.prompt_tokens), 0).label("total_prompt_tokens"),
                func.coalesce(func.sum(AIUsageLog.completion_tokens), 0).label(
                    "total_completion_tokens"
                ),
                func.coalesce(func.sum(AIUsageLog.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(AIUsageLog.estimated_cost), Decimal("0.0")).label(
                    "total_cost"
                ),
            )
            .where(
                AIUsageLog.user_id == user_id,
                AIUsageLog.created_at >= start_time,
            )
        )
        result = await self.session.execute(stmt)
        row = result.one()

        return UsageSummary(
            total_requests=int(row.total_requests or 0),
            total_prompt_tokens=int(row.total_prompt_tokens or 0),
            total_completion_tokens=int(row.total_completion_tokens or 0),
            total_tokens=int(row.total_tokens or 0),
            total_cost=float(row.total_cost or 0.0),
        )

    async def list_user_logs(
        self,
        user_id: uuid.UUID,
        limit: int = 50,
        skip: int = 0,
    ) -> list[AIUsageLog]:
        """Fetch paginated AI usage logs for a specific user, ordered newest first.

        Args:
            user_id: Student/user UUID.
            limit: Maximum records to return (defaults to 50, clamped to 1..100).
            skip: Number of records to skip (offset).

        Returns:
            List of AIUsageLog instances ordered by created_at DESC.
        """
        clamped_limit = max(1, min(limit, 100))
        offset = max(0, skip)

        stmt = (
            select(AIUsageLog)
            .where(AIUsageLog.user_id == user_id)
            .order_by(AIUsageLog.created_at.desc())
            .offset(offset)
            .limit(clamped_limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


def get_ai_usage_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AIUsageRepository:
    """FastAPI dependency provider for AIUsageRepository with an active database session."""
    return AIUsageRepository(session)


__all__ = [
    "VALID_REQUEST_STATUSES",
    "AIUsageLog",
    "AIUsageRepository",
    "UsageSummary",
    "get_ai_usage_repo",
]
