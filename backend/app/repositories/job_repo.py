"""Background job repository and SQLAlchemy model for async AI tasks (TASK-M5-01 / Invariant 8 & 9).

Adheres strictly to:
- Invariant 2: Repositories encapsulate database operations.
- Invariant 6: Strict tenant isolation (scoped by user_id).
- Invariant 8 & 9: Long AI tasks track status in PostgreSQL background_jobs table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, get_db
from app.repositories.user_repo import User

# Mapping between application/task status names and PostgreSQL check constraint values
STATUS_TO_DB: dict[str, str] = {
    "running": "processing",
    "completed": "succeeded",
}

STATUS_TO_API: dict[str, str] = {
    "processing": "running",
    "succeeded": "completed",
}


class BackgroundJob(Base):
    """Background job model representing background_jobs table."""

    __tablename__ = "background_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    progress: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, default=lambda: str(uuid.uuid4())
    )
    attempt_count: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, default=3, nullable=False)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
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

    user: Mapped[User | None] = relationship("User", lazy="select")

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_background_jobs_idempotency_key"),
        CheckConstraint(
            "status IN ('queued', 'processing', 'succeeded', 'failed', 'cancelled')",
            name="ck_background_jobs_status",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_background_jobs_progress"),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0",
            name="ck_background_jobs_attempts",
        ),
    )

    @property
    def progress_percent(self) -> int:
        """Alias property for progress column."""
        return self.progress

    @progress_percent.setter
    def progress_percent(self, value: int) -> None:
        self.progress = value

    @property
    def result_payload(self) -> dict[str, Any] | None:
        """Alias property for result column."""
        return self.result

    @result_payload.setter
    def result_payload(self, value: dict[str, Any] | None) -> None:
        self.result = value

    @property
    def completed_at(self) -> datetime | None:
        """Alias property for finished_at column."""
        return self.finished_at

    @completed_at.setter
    def completed_at(self, value: datetime | None) -> None:
        self.finished_at = value

    @property
    def api_status(self) -> str:
        """Normalized status string for API consumers ('running', 'completed', etc.)."""
        return STATUS_TO_API.get(self.status, self.status)


class JobRepository:
    """Repository handling database operations for BackgroundJob model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_job(
        self,
        user_id: uuid.UUID,
        job_type: str,
        idempotency_key: str | None = None,
    ) -> BackgroundJob:
        """Create and persist a new background job in 'queued' status.

        Args:
            user_id: Requesting student UUID.
            job_type: Feature identifier ('document_summarization', etc.).
            idempotency_key: Optional custom idempotency key.

        Returns:
            Created BackgroundJob instance.
        """
        key = idempotency_key or f"{job_type}:{user_id}:{uuid.uuid4()}"
        job = BackgroundJob(
            user_id=user_id,
            job_type=job_type,
            status="queued",
            progress=0,
            idempotency_key=key,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def get_job(
        self,
        user_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> BackgroundJob | None:
        """Fetch a background job by ID ensuring strict tenant isolation.

        Args:
            user_id: Authenticated student UUID.
            job_id: Job UUID.

        Returns:
            BackgroundJob instance if found and owned by user_id, else None.
        """
        stmt = select(BackgroundJob).where(
            BackgroundJob.id == job_id,
            BackgroundJob.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_job_status(
        self,
        job_id: uuid.UUID,
        status: str,
        progress_percent: int = 0,
        result_payload: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> BackgroundJob | None:
        """Update status, progress, and results of a background job.

        Args:
            job_id: Job UUID.
            status: New status ('running', 'completed', 'failed', 'cancelled', etc.).
            progress_percent: Completion percentage (0 to 100).
            result_payload: Optional JSON result data dictionary.
            error_code: Optional machine-readable error code.
            error_message: Optional error description.

        Returns:
            Updated BackgroundJob instance, or None if job not found.
        """
        stmt = select(BackgroundJob).where(BackgroundJob.id == job_id)
        res = await self.session.execute(stmt)
        job = res.scalar_one_or_none()
        if not job:
            return None

        clean_status = status.lower().strip()
        db_status = STATUS_TO_DB.get(clean_status, clean_status)

        job.status = db_status
        job.progress = max(0, min(100, int(progress_percent)))

        if result_payload is not None:
            job.result = result_payload
        if error_code is not None:
            job.error_code = error_code
        if error_message is not None:
            job.error_message = error_message

        now = datetime.now(UTC)
        if db_status in ("processing",) and not job.started_at:
            job.started_at = now
        if db_status in ("succeeded", "failed", "cancelled"):
            job.finished_at = now

        await self.session.flush()
        return job


async def get_job_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JobRepository:
    """FastAPI dependency provider for JobRepository."""
    return JobRepository(session)
