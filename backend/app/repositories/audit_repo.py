"""Audit repository and SQLAlchemy models for security logging and login attempt tracking (NFR04)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Boolean, DateTime, String, func, select
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, get_db


class LoginAttempt(Base):
    """Audit record representing login_attempts table."""

    __tablename__ = "login_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    identifier_hash: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    ip_address: Mapped[str | None] = mapped_column(
        INET().with_variant(String(45), "sqlite"), nullable=True, index=True
    )
    succeeded: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuditRepository:
    """Repository handling persistent security auditing and login attempt throttling (NFR04)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_login_attempt(
        self,
        identifier_hash: str,
        ip_address: str | None = None,
        succeeded: bool = False,
        attempted_at: datetime | None = None,
    ) -> LoginAttempt:
        """Persist a new login attempt audit record.

        Args:
            identifier_hash: SHA-256 hash of the normalized login identifier (e.g. email).
            ip_address: Optional client IP address.
            succeeded: True if authentication succeeded, False otherwise.
            attempted_at: Optional explicit timestamp (defaults to current UTC time).

        Returns:
            The persisted LoginAttempt model instance.
        """
        attempt = LoginAttempt(
            identifier_hash=identifier_hash,
            ip_address=ip_address,
            succeeded=succeeded,
            attempted_at=attempted_at or datetime.now(UTC),
        )
        self.session.add(attempt)
        await self.session.flush()
        return attempt

    async def count_recent_failures(
        self,
        identifier_hash: str,
        window_minutes: int = 15,
        now: datetime | None = None,
    ) -> int:
        """Count consecutive failed login attempts for an identifier since (now - window_minutes).

        If a successful login occurred within the window, only failures after
        the most recent successful login are counted.

        Args:
            identifier_hash: SHA-256 hash of the normalized login identifier.
            window_minutes: Sliding time window in minutes (defaults to 15).
            now: Optional current timestamp (defaults to current UTC time).

        Returns:
            Integer count of recent consecutive failed attempts.
        """
        current_time = now or datetime.now(UTC)
        window_start = current_time - timedelta(minutes=window_minutes)

        # 1. Find timestamp of most recent successful login for this identifier
        success_stmt = (
            select(func.max(LoginAttempt.attempted_at))
            .where(
                LoginAttempt.identifier_hash == identifier_hash,
                LoginAttempt.succeeded.is_(True),
            )
        )
        success_res = await self.session.execute(success_stmt)
        latest_success = success_res.scalar_one_or_none()

        stmt = select(func.count(LoginAttempt.id)).where(
            LoginAttempt.identifier_hash == identifier_hash,
            LoginAttempt.succeeded.is_(False),
        )

        if latest_success is not None:
            if latest_success.tzinfo is None:
                latest_success = latest_success.replace(tzinfo=UTC)
            else:
                latest_success = latest_success.astimezone(UTC)

            if latest_success > window_start:
                stmt = stmt.where(LoginAttempt.attempted_at > latest_success)
            else:
                stmt = stmt.where(LoginAttempt.attempted_at >= window_start)
        else:
            stmt = stmt.where(LoginAttempt.attempted_at >= window_start)

        res = await self.session.execute(stmt)
        return int(res.scalar_one() or 0)

    async def get_lockout_remaining_seconds(
        self,
        identifier_hash: str,
        max_failures: int = 5,
        window_minutes: int = 15,
        now: datetime | None = None,
    ) -> int:
        """Calculate remaining lockout duration in seconds, or 0 if not locked out.

        A user is locked out when recent consecutive failures reach or exceed max_failures.
        The lockout expires when failures age out of the sliding window.

        Args:
            identifier_hash: SHA-256 hash of the normalized login identifier.
            max_failures: Maximum failed attempts allowed before throttling (defaults to 5).
            window_minutes: Sliding time window in minutes (defaults to 15).
            now: Optional current timestamp (defaults to current UTC time).

        Returns:
            Integer number of seconds remaining before lockout expires, or 0 if not locked out.
        """
        current_time = now or datetime.now(UTC)
        window_start = current_time - timedelta(minutes=window_minutes)

        # 1. Find timestamp of most recent successful login for this identifier
        success_stmt = (
            select(func.max(LoginAttempt.attempted_at))
            .where(
                LoginAttempt.identifier_hash == identifier_hash,
                LoginAttempt.succeeded.is_(True),
            )
        )
        success_res = await self.session.execute(success_stmt)
        latest_success = success_res.scalar_one_or_none()

        stmt = select(LoginAttempt.attempted_at).where(
            LoginAttempt.identifier_hash == identifier_hash,
            LoginAttempt.succeeded.is_(False),
        )

        if latest_success is not None:
            if latest_success.tzinfo is None:
                latest_success = latest_success.replace(tzinfo=UTC)
            else:
                latest_success = latest_success.astimezone(UTC)

            if latest_success > window_start:
                stmt = stmt.where(LoginAttempt.attempted_at > latest_success)
            else:
                stmt = stmt.where(LoginAttempt.attempted_at >= window_start)
        else:
            stmt = stmt.where(LoginAttempt.attempted_at >= window_start)

        stmt = stmt.order_by(LoginAttempt.attempted_at.desc())
        res = await self.session.execute(stmt)
        failure_times = list(res.scalars().all())

        if len(failure_times) < max_failures:
            return 0

        # The lockout threshold attempt is the max_failures-th newest failure
        # (index max_failures - 1 in descending order).
        # When this attempt is window_minutes old, count of failures within window drops below max_failures.
        threshold_time = failure_times[max_failures - 1]
        if threshold_time.tzinfo is None:
            threshold_time = threshold_time.replace(tzinfo=UTC)
        else:
            threshold_time = threshold_time.astimezone(UTC)

        lockout_expires_at = threshold_time + timedelta(minutes=window_minutes)
        remaining = int((lockout_expires_at - current_time).total_seconds())

        return max(1, remaining) if remaining > 0 else 0


def get_audit_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AuditRepository:
    """FastAPI dependency provider for AuditRepository with an active database session."""
    return AuditRepository(session)
