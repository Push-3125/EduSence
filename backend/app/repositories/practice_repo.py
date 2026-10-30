"""Practice attempts and answer results repository with SQLAlchemy models (TASK-M5-03 / US 2.15).

Adheres strictly to:
- Invariant 2: Repository encapsulates database persistence; no business logic.
- Invariant 6: Strict tenant isolation. Every practice attempt operation filters by user_id.
- Schema alignment: matches baseline PostgreSQL schema for practice_attempts and practice_answer_results tables.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload

from app.core.database import Base, get_db
from app.repositories.question_repo import Question, QuestionBank
from app.repositories.user_repo import User

# ---------------------------------------------------------------------------
# SQLAlchemy Models
# ---------------------------------------------------------------------------


class PracticeAttempt(Base):
    """Practice attempt model representing practice_attempts table."""

    __tablename__ = "practice_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question_bank_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("question_banks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        server_default="in_progress",
        default="in_progress",
        nullable=False,
    )
    total_score: Mapped[float | None] = mapped_column(
        Numeric(10, 2, asdecimal=False), nullable=True
    )
    max_score: Mapped[float | None] = mapped_column(
        Numeric(10, 2, asdecimal=False), nullable=True, default=100.0
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    graded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        onupdate=func.now(),
        nullable=False,
    )

    results: Mapped[list[PracticeAnswerResult]] = relationship(
        "PracticeAnswerResult",
        back_populates="attempt",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="PracticeAnswerResult.created_at.asc()",
    )
    bank: Mapped[QuestionBank] = relationship(
        "QuestionBank",
        lazy="selectin",
    )
    user: Mapped[User] = relationship(
        "User",
        lazy="select",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'submitted', 'graded', 'failed')",
            name="ck_practice_attempts_status",
        ),
        CheckConstraint(
            "(total_score IS NULL OR total_score >= 0) "
            "AND (max_score IS NULL OR max_score >= 0) "
            "AND (total_score IS NULL OR max_score IS NULL OR total_score <= max_score)",
            name="ck_practice_attempts_scores",
        ),
    )


class PracticeAnswerResult(Base):
    """Practice answer result model representing practice_answer_results table."""

    __tablename__ = "practice_answer_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("practice_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float | None] = mapped_column(
        Numeric(10, 2, asdecimal=False), nullable=True
    )
    max_score: Mapped[float | None] = mapped_column(
        Numeric(10, 2, asdecimal=False), nullable=True
    )
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubric_breakdown: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    reference_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    grader_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    graded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        onupdate=func.now(),
        nullable=False,
    )

    attempt: Mapped[PracticeAttempt] = relationship(
        "PracticeAttempt",
        back_populates="results",
        lazy="select",
    )
    question: Mapped[Question] = relationship(
        "Question",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "question_id",
            name="uq_practice_answer_results_attempt_question",
        ),
        CheckConstraint(
            "(score IS NULL OR score >= 0) "
            "AND (max_score IS NULL OR max_score >= 0) "
            "AND (score IS NULL OR max_score IS NULL OR score <= max_score)",
            name="ck_practice_answer_results_scores",
        ),
    )


# ---------------------------------------------------------------------------
# Repository Implementation
# ---------------------------------------------------------------------------


class PracticeRepository:
    """Repository handling database operations for practice attempts and answer results."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_attempt(
        self,
        user_id: uuid.UUID,
        question_bank_id: uuid.UUID,
        max_score: float = 100.0,
    ) -> PracticeAttempt:
        """Create and persist a new practice attempt in 'in_progress' state.

        Args:
            user_id: Owner student UUID (strict tenant isolation).
            question_bank_id: Target question bank UUID.
            max_score: Maximum achievable total score (default 100.0).

        Returns:
            Created PracticeAttempt instance with associations loaded.
        """
        attempt = PracticeAttempt(
            user_id=user_id,
            question_bank_id=question_bank_id,
            status="in_progress",
            max_score=max_score,
            total_score=None,
        )
        self.session.add(attempt)
        await self.session.flush()

        stmt = (
            select(PracticeAttempt)
            .where(PracticeAttempt.id == attempt.id)
            .options(
                selectinload(PracticeAttempt.results),
                selectinload(PracticeAttempt.bank).selectinload(QuestionBank.questions),
            )
        )
        res = await self.session.execute(stmt)
        return res.scalar_one()

    async def get_attempt(
        self,
        user_id: uuid.UUID,
        attempt_id: uuid.UUID,
    ) -> PracticeAttempt | None:
        """Fetch a practice attempt strictly enforcing tenant isolation.

        Args:
            user_id: Authenticated student UUID.
            attempt_id: Target attempt UUID.

        Returns:
            PracticeAttempt with results and questions eagerly loaded if owned by user, else None.
        """
        stmt = (
            select(PracticeAttempt)
            .where(
                PracticeAttempt.id == attempt_id,
                PracticeAttempt.user_id == user_id,
            )
            .options(
                selectinload(PracticeAttempt.results).selectinload(PracticeAnswerResult.question),
                selectinload(PracticeAttempt.bank).selectinload(QuestionBank.questions),
            )
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def list_attempts(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[PracticeAttempt], int]:
        """List practice attempts for a student with optional question bank filter.

        Args:
            user_id: Authenticated student UUID (strict tenant isolation).
            bank_id: Optional question bank UUID filter.
            skip: Pagination offset.
            limit: Maximum items to return.

        Returns:
            Tuple of (list of PracticeAttempt, total_count).
        """
        conditions = [PracticeAttempt.user_id == user_id]
        if bank_id is not None:
            conditions.append(PracticeAttempt.question_bank_id == bank_id)

        count_stmt = select(func.count(PracticeAttempt.id)).where(*conditions)
        total_count = await self.session.scalar(count_stmt) or 0

        stmt = (
            select(PracticeAttempt)
            .where(*conditions)
            .order_by(PracticeAttempt.created_at.desc())
            .offset(skip)
            .limit(limit)
            .options(
                selectinload(PracticeAttempt.results),
                selectinload(PracticeAttempt.bank),
            )
        )
        res = await self.session.execute(stmt)
        items = list(res.scalars().all())
        return items, total_count

    async def save_answer_results(
        self,
        attempt_id: uuid.UUID,
        results_data: list[dict[str, Any]],
        total_score: float,
        status: str = "graded",
    ) -> PracticeAttempt:
        """Save student answer evaluation results and update practice attempt status.

        Args:
            attempt_id: Practice attempt UUID.
            results_data: List of dictionaries matching PracticeAnswerResult columns.
            total_score: Overall calculated attempt score.
            status: Target attempt status ('graded' or 'submitted').

        Returns:
            Updated PracticeAttempt entity with results and questions eagerly loaded.
        """
        stmt = (
            select(PracticeAttempt)
            .where(PracticeAttempt.id == attempt_id)
            .options(selectinload(PracticeAttempt.results))
        )
        res = await self.session.execute(stmt)
        attempt = res.scalar_one_or_none()
        if attempt is None:
            raise ValueError(f"Practice attempt {attempt_id} not found.")

        now = datetime.now(UTC)
        attempt.total_score = total_score
        attempt.status = status
        attempt.graded_at = now
        if attempt.submitted_at is None:
            attempt.submitted_at = now

        # Remove existing results if any to satisfy unique constraint
        for old_result in list(attempt.results):
            await self.session.delete(old_result)
        await self.session.flush()

        for data in results_data:
            answer_result = PracticeAnswerResult(
                attempt_id=attempt.id,
                question_id=data["question_id"],
                answer_text=data.get("answer_text"),
                score=data.get("score"),
                max_score=data.get("max_score"),
                feedback=data.get("feedback"),
                rationale=data.get("rationale"),
                rubric_breakdown=data.get("rubric_breakdown"),
                reference_answer=data.get("reference_answer"),
                grader_model=data.get("grader_model"),
                graded_at=now,
            )
            self.session.add(answer_result)

        await self.session.flush()

        # Reload with full associations
        reload_stmt = (
            select(PracticeAttempt)
            .where(PracticeAttempt.id == attempt_id)
            .options(
                selectinload(PracticeAttempt.results).selectinload(PracticeAnswerResult.question),
                selectinload(PracticeAttempt.bank).selectinload(QuestionBank.questions),
            )
        )
        reloaded_res = await self.session.execute(reload_stmt)
        return reloaded_res.scalar_one()


# ---------------------------------------------------------------------------
# Dependency Provider
# ---------------------------------------------------------------------------


async def get_practice_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PracticeRepository:
    """FastAPI dependency yielding a PracticeRepository instance."""
    return PracticeRepository(session)


__all__ = [
    "PracticeAnswerResult",
    "PracticeAttempt",
    "PracticeRepository",
    "get_practice_repo",
]
