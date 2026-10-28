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
from dataclasses import dataclass
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


@dataclass
class FeatureUsageBreakdown:
    """Usage metrics aggregated by AI feature."""

    feature: str
    total_requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    average_latency_ms: float


@dataclass
class ModelUsageBreakdown:
    """Usage metrics aggregated by AI model."""

    model_name: str
    total_requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float


@dataclass
class StatusUsageBreakdown:
    """Request counts aggregated by request outcome status."""

    request_status: str
    total_requests: int


@dataclass
class SystemAnalyticsResult:
    """System-wide AI usage telemetry analytics and breakdowns."""

    total_requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    average_latency_ms: float
    by_feature: list[FeatureUsageBreakdown]
    by_model: list[ModelUsageBreakdown]
    by_status: list[StatusUsageBreakdown]


@dataclass
class TopUserUsageItem:
    """AI token and request consumption summary for top users."""

    user_id: uuid.UUID
    email: str
    full_name: str
    total_requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float


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

    async def get_system_analytics(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> SystemAnalyticsResult:
        """Calculate system-wide AI usage analytics and breakdowns over a time window.

        Args:
            start_time: Start datetime (inclusive).
            end_time: End datetime (inclusive).

        Returns:
            SystemAnalyticsResult with overall metrics and breakdowns.
        """
        # 1. Overall totals
        stmt_overall = (
            select(
                func.count(AIUsageLog.id).label("total_requests"),
                func.coalesce(func.sum(AIUsageLog.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(AIUsageLog.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(AIUsageLog.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(AIUsageLog.estimated_cost), Decimal("0.0")).label(
                    "estimated_cost"
                ),
                func.coalesce(func.avg(AIUsageLog.latency_ms), 0.0).label("avg_latency"),
            )
            .where(
                AIUsageLog.created_at >= start_time,
                AIUsageLog.created_at <= end_time,
            )
        )
        res_overall = await self.session.execute(stmt_overall)
        row = res_overall.one()

        # 2. Breakdown by feature
        stmt_feature = (
            select(
                AIUsageLog.feature,
                func.count(AIUsageLog.id).label("total_requests"),
                func.coalesce(func.sum(AIUsageLog.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(AIUsageLog.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(AIUsageLog.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(AIUsageLog.estimated_cost), Decimal("0.0")).label(
                    "estimated_cost"
                ),
                func.coalesce(func.avg(AIUsageLog.latency_ms), 0.0).label("avg_latency"),
            )
            .where(
                AIUsageLog.created_at >= start_time,
                AIUsageLog.created_at <= end_time,
            )
            .group_by(AIUsageLog.feature)
            .order_by(func.count(AIUsageLog.id).desc())
        )
        res_feature = await self.session.execute(stmt_feature)
        by_feature = [
            FeatureUsageBreakdown(
                feature=r.feature,
                total_requests=int(r.total_requests or 0),
                prompt_tokens=int(r.prompt_tokens or 0),
                completion_tokens=int(r.completion_tokens or 0),
                total_tokens=int(r.total_tokens or 0),
                estimated_cost=float(r.estimated_cost or 0.0),
                average_latency_ms=round(float(r.avg_latency or 0.0), 2),
            )
            for r in res_feature.all()
        ]

        # 3. Breakdown by model
        stmt_model = (
            select(
                AIUsageLog.model_name,
                func.count(AIUsageLog.id).label("total_requests"),
                func.coalesce(func.sum(AIUsageLog.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(AIUsageLog.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(AIUsageLog.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(AIUsageLog.estimated_cost), Decimal("0.0")).label(
                    "estimated_cost"
                ),
            )
            .where(
                AIUsageLog.created_at >= start_time,
                AIUsageLog.created_at <= end_time,
            )
            .group_by(AIUsageLog.model_name)
            .order_by(func.count(AIUsageLog.id).desc())
        )
        res_model = await self.session.execute(stmt_model)
        by_model = [
            ModelUsageBreakdown(
                model_name=r.model_name,
                total_requests=int(r.total_requests or 0),
                prompt_tokens=int(r.prompt_tokens or 0),
                completion_tokens=int(r.completion_tokens or 0),
                total_tokens=int(r.total_tokens or 0),
                estimated_cost=float(r.estimated_cost or 0.0),
            )
            for r in res_model.all()
        ]

        # 4. Breakdown by status
        stmt_status = (
            select(
                AIUsageLog.request_status,
                func.count(AIUsageLog.id).label("total_requests"),
            )
            .where(
                AIUsageLog.created_at >= start_time,
                AIUsageLog.created_at <= end_time,
            )
            .group_by(AIUsageLog.request_status)
            .order_by(func.count(AIUsageLog.id).desc())
        )
        res_status = await self.session.execute(stmt_status)
        by_status = [
            StatusUsageBreakdown(
                request_status=r.request_status,
                total_requests=int(r.total_requests or 0),
            )
            for r in res_status.all()
        ]

        return SystemAnalyticsResult(
            total_requests=int(row.total_requests or 0),
            prompt_tokens=int(row.prompt_tokens or 0),
            completion_tokens=int(row.completion_tokens or 0),
            total_tokens=int(row.total_tokens or 0),
            estimated_cost=float(row.estimated_cost or 0.0),
            average_latency_ms=round(float(row.avg_latency or 0.0), 2),
            by_feature=by_feature,
            by_model=by_model,
            by_status=by_status,
        )

    async def get_top_users(
        self,
        limit: int = 10,
        start_time: datetime | None = None,
    ) -> list[TopUserUsageItem]:
        """Fetch top users ranked by total AI token consumption.

        Args:
            limit: Maximum users to return (defaults to 10, clamped 1..100).
            start_time: Optional start timestamp filter.

        Returns:
            List of TopUserUsageItem instances ordered by total_tokens DESC.
        """
        from app.repositories.user_repo import User, UserProfile

        clamped_limit = max(1, min(limit, 100))
        stmt = (
            select(
                AIUsageLog.user_id,
                User.email,
                func.coalesce(UserProfile.full_name, User.email).label("full_name"),
                func.count(AIUsageLog.id).label("total_requests"),
                func.coalesce(func.sum(AIUsageLog.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(AIUsageLog.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(AIUsageLog.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(AIUsageLog.estimated_cost), Decimal("0.0")).label(
                    "estimated_cost"
                ),
            )
            .join(User, AIUsageLog.user_id == User.id)
            .outerjoin(UserProfile, User.id == UserProfile.user_id)
        )
        if start_time is not None:
            stmt = stmt.where(AIUsageLog.created_at >= start_time)

        stmt = (
            stmt.group_by(AIUsageLog.user_id, User.email, UserProfile.full_name)
            .order_by(func.sum(AIUsageLog.total_tokens).desc())
            .limit(clamped_limit)
        )

        result = await self.session.execute(stmt)
        return [
            TopUserUsageItem(
                user_id=r.user_id,
                email=r.email,
                full_name=r.full_name,
                total_requests=int(r.total_requests or 0),
                prompt_tokens=int(r.prompt_tokens or 0),
                completion_tokens=int(r.completion_tokens or 0),
                total_tokens=int(r.total_tokens or 0),
                estimated_cost=float(r.estimated_cost or 0.0),
            )
            for r in result.all()
        ]

    async def get_telemetry_logs(
        self,
        limit: int = 50,
        skip: int = 0,
        feature: str | None = None,
        request_status: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> tuple[list[AIUsageLog], int]:
        """Fetch paginated AI telemetry logs with filters and total count.

        Args:
            limit: Maximum logs to return (clamped to 1..100).
            skip: Number of records to skip.
            feature: Optional feature filter.
            request_status: Optional request status filter.
            user_id: Optional user UUID filter.

        Returns:
            Tuple of (list of AIUsageLog, total matching records count).
        """
        clamped_limit = max(1, min(limit, 100))
        offset = max(0, skip)

        conditions = []
        if feature:
            conditions.append(AIUsageLog.feature == feature.strip().lower())
        if request_status:
            conditions.append(AIUsageLog.request_status == request_status.strip().lower())
        if user_id is not None:
            conditions.append(AIUsageLog.user_id == user_id)

        count_stmt = select(func.count(AIUsageLog.id))
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total_count = (await self.session.execute(count_stmt)).scalar() or 0

        fetch_stmt = select(AIUsageLog)
        if conditions:
            fetch_stmt = fetch_stmt.where(*conditions)
        fetch_stmt = (
            fetch_stmt.order_by(AIUsageLog.created_at.desc())
            .offset(offset)
            .limit(clamped_limit)
        )

        logs = list((await self.session.execute(fetch_stmt)).scalars().all())
        return logs, total_count


def get_ai_usage_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AIUsageRepository:
    """FastAPI dependency provider for AIUsageRepository with an active database session."""
    return AIUsageRepository(session)


__all__ = [
    "VALID_REQUEST_STATUSES",
    "AIUsageLog",
    "AIUsageRepository",
    "FeatureUsageBreakdown",
    "ModelUsageBreakdown",
    "StatusUsageBreakdown",
    "SystemAnalyticsResult",
    "TopUserUsageItem",
    "UsageSummary",
    "get_ai_usage_repo",
]
