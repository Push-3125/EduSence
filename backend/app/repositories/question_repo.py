"""Question bank and question repository with SQLAlchemy models (TASK-M5-02 / US 2.15 / US 2.16).

Adheres strictly to:
- Invariant 2: Repository encapsulates database persistence; no business logic.
- Invariant 6: Strict tenant isolation. Every question bank and question operation filters by user_id.
- Schema alignment: matches baseline PostgreSQL schema for question_banks and questions tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload

from app.core.database import Base, get_db
from app.repositories.course_repo import Course
from app.repositories.document_repo import Document
from app.repositories.user_repo import User

# ---------------------------------------------------------------------------
# SQLAlchemy Models
# ---------------------------------------------------------------------------


class QuestionBank(Base):
    """Question bank model representing question_banks table."""

    __tablename__ = "question_banks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    questions: Mapped[list[Question]] = relationship(
        "Question",
        back_populates="question_bank",
        order_by="Question.created_at.asc()",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    user: Mapped[User] = relationship("User", lazy="select")
    course: Mapped[Course | None] = relationship("Course", lazy="select")
    document: Mapped[Document | None] = relationship("Document", lazy="select")


class Question(Base):
    """Question model representing questions table."""

    __tablename__ = "questions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    question_bank_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("question_banks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    correct_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty: Mapped[str] = mapped_column(
        String(10), server_default="medium", default="medium", nullable=False
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

    question_bank: Mapped[QuestionBank] = relationship(
        "QuestionBank",
        back_populates="questions",
        lazy="select",
    )

    __table_args__ = (
        CheckConstraint(
            "question_type IN ('multiple_choice', 'essay')",
            name="ck_questions_type",
        ),
        CheckConstraint(
            "difficulty IN ('easy', 'medium', 'hard')",
            name="ck_questions_difficulty",
        ),
        CheckConstraint(
            "(question_type = 'multiple_choice' AND options IS NOT NULL AND jsonb_typeof(options) = 'array') OR (question_type = 'essay')",
            name="ck_questions_options",
        ),
    )


# ---------------------------------------------------------------------------
# Repository Implementation
# ---------------------------------------------------------------------------


class QuestionRepository:
    """Repository handling database operations for question banks and questions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_bank(
        self,
        user_id: uuid.UUID,
        name: str,
        description: str | None = None,
        course_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
    ) -> QuestionBank:
        """Create and persist a new question bank.

        Args:
            user_id: Owner student UUID (strict tenant isolation).
            name: Title of the question bank.
            description: Optional description of the bank.
            course_id: Optional linked course UUID.
            document_id: Optional linked document UUID.

        Returns:
            Created QuestionBank instance.
        """
        bank = QuestionBank(
            user_id=user_id,
            name=name.strip(),
            description=description.strip() if description else None,
            course_id=course_id,
            document_id=document_id,
        )
        self.session.add(bank)
        await self.session.flush()
        await self.session.refresh(bank, ["questions"])
        return bank

    async def get_bank(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID,
    ) -> QuestionBank | None:
        """Fetch a question bank strictly enforcing tenant isolation.

        Args:
            user_id: Authenticated student UUID.
            bank_id: Target question bank UUID.

        Returns:
            QuestionBank with eagerly loaded questions if owned by user, else None.
        """
        stmt = (
            select(QuestionBank)
            .options(selectinload(QuestionBank.questions))
            .where(QuestionBank.id == bank_id, QuestionBank.user_id == user_id)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def list_banks(
        self,
        user_id: uuid.UUID,
        skip: int = 0,
        limit: int = 50,
        course_id: uuid.UUID | None = None,
    ) -> tuple[list[QuestionBank], int]:
        """List question banks owned by user with pagination and optional course filter.

        Args:
            user_id: Authenticated student UUID.
            skip: Offset items count.
            limit: Maximum items to return.
            course_id: Optional course filter UUID.

        Returns:
            Tuple of (list of QuestionBank, total_count).
        """
        count_stmt = select(func.count(QuestionBank.id)).where(QuestionBank.user_id == user_id)
        if course_id is not None:
            count_stmt = count_stmt.where(QuestionBank.course_id == course_id)
        total = await self.session.scalar(count_stmt) or 0

        stmt = (
            select(QuestionBank)
            .options(selectinload(QuestionBank.questions))
            .where(QuestionBank.user_id == user_id)
        )
        if course_id is not None:
            stmt = stmt.where(QuestionBank.course_id == course_id)
        stmt = stmt.order_by(QuestionBank.created_at.desc()).offset(skip).limit(limit)
        res = await self.session.execute(stmt)
        return list(res.scalars().all()), total

    async def delete_bank(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID,
    ) -> bool:
        """Delete a question bank and cascade all questions, enforcing ownership.

        Args:
            user_id: Authenticated student UUID.
            bank_id: Question bank UUID.

        Returns:
            True if deleted, False if not found or access denied.
        """
        bank = await self.get_bank(user_id=user_id, bank_id=bank_id)
        if bank is None:
            return False

        await self.session.delete(bank)
        await self.session.flush()
        return True

    async def add_questions(
        self,
        bank_id: uuid.UUID,
        questions_data: list[dict[str, Any]],
    ) -> list[Question]:
        """Add multiple questions to a question bank.

        Args:
            bank_id: Target question bank UUID.
            questions_data: List of dicts representing question fields.

        Returns:
            List of created Question entities.
        """
        created: list[Question] = []
        for item in questions_data:
            q = Question(
                question_bank_id=bank_id,
                question_type=item["question_type"],
                content=item["content"],
                options=item.get("options"),
                correct_answer=item.get("correct_answer"),
                explanation=item.get("explanation"),
                difficulty=item.get("difficulty", "medium"),
            )
            self.session.add(q)
            created.append(q)

        await self.session.flush()
        return created

    async def get_question(
        self,
        user_id: uuid.UUID,
        question_id: uuid.UUID,
    ) -> Question | None:
        """Fetch a single question verifying bank ownership by user.

        Args:
            user_id: Authenticated student UUID.
            question_id: Target question UUID.

        Returns:
            Question if found and bank owned by user, else None.
        """
        stmt = (
            select(Question)
            .join(QuestionBank, Question.question_bank_id == QuestionBank.id)
            .where(Question.id == question_id, QuestionBank.user_id == user_id)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def delete_question(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID,
        question_id: uuid.UUID,
    ) -> bool:
        """Delete a question from a bank verifying ownership.

        Args:
            user_id: Authenticated student UUID.
            bank_id: Question bank UUID.
            question_id: Target question UUID.

        Returns:
            True if deleted, False if not found or unauthorized.
        """
        stmt = (
            select(Question)
            .join(QuestionBank, Question.question_bank_id == QuestionBank.id)
            .where(
                Question.id == question_id,
                Question.question_bank_id == bank_id,
                QuestionBank.user_id == user_id,
            )
        )
        res = await self.session.execute(stmt)
        question = res.scalar_one_or_none()
        if question is None:
            return False

        await self.session.delete(question)
        await self.session.flush()
        return True


# ---------------------------------------------------------------------------
# Dependency Provider
# ---------------------------------------------------------------------------


async def get_question_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> QuestionRepository:
    """FastAPI dependency yielding a QuestionRepository instance."""
    return QuestionRepository(session)
