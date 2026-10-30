"""Document summary repository and SQLAlchemy model (TASK-M5-01 / US 2.14).

Adheres strictly to:
- Invariant 2: Repositories encapsulate database operations.
- Invariant 6: Strict tenant isolation. Every query and operation filters by user_id.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, get_db
from app.repositories.document_repo import Document
from app.repositories.user_repo import User


class DocumentSummary(Base):
    """Document summary model representing document_summaries table."""

    __tablename__ = "document_summaries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    summary_type: Mapped[str] = mapped_column(
        String(20), default="general", nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    document: Mapped[Document] = relationship("Document", lazy="select")
    user: Mapped[User] = relationship("User", lazy="select")

    __table_args__ = (
        CheckConstraint(
            "summary_type IN ('brief', 'general', 'detailed')",
            name="ck_document_summaries_type",
        ),
    )


class SummaryRepository:
    """Repository handling database operations for DocumentSummary model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_summary(
        self,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        summary_type: str,
        content: str,
        model_name: str | None = None,
    ) -> DocumentSummary:
        """Create and persist a new document summary.

        Args:
            document_id: Owner document UUID.
            user_id: Owner student UUID.
            summary_type: Depth type ('brief', 'general', 'detailed').
            content: Generated summary text content.
            model_name: Optional LLM model name.

        Returns:
            Created DocumentSummary instance.
        """
        summary = DocumentSummary(
            document_id=document_id,
            user_id=user_id,
            summary_type=summary_type,
            content=content,
            model_name=model_name,
        )
        self.session.add(summary)
        await self.session.flush()
        return summary

    async def get_summary(
        self,
        user_id: uuid.UUID,
        summary_id: uuid.UUID,
    ) -> DocumentSummary | None:
        """Fetch summary by ID ensuring strict tenant isolation.

        Args:
            user_id: Authenticated student UUID.
            summary_id: Summary UUID.

        Returns:
            DocumentSummary instance if found and owned by user_id, else None.
        """
        stmt = select(DocumentSummary).where(
            DocumentSummary.id == summary_id,
            DocumentSummary.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_summaries_by_document(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> list[DocumentSummary]:
        """List all summaries for a specific document scoped to authenticated user.

        Args:
            user_id: Authenticated student UUID.
            document_id: Document UUID.

        Returns:
            List of DocumentSummary instances ordered by created_at descending.
        """
        stmt = (
            select(DocumentSummary)
            .where(
                DocumentSummary.document_id == document_id,
                DocumentSummary.user_id == user_id,
            )
            .order_by(DocumentSummary.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def delete_summary(
        self,
        user_id: uuid.UUID,
        summary_id: uuid.UUID,
    ) -> bool:
        """Delete summary by ID ensuring strict tenant isolation.

        Args:
            user_id: Authenticated student UUID.
            summary_id: Summary UUID.

        Returns:
            True if deleted, False if not found or unauthorized.
        """
        summary = await self.get_summary(user_id=user_id, summary_id=summary_id)
        if not summary:
            return False
        await self.session.delete(summary)
        await self.session.flush()
        return True


async def get_summary_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> SummaryRepository:
    """FastAPI dependency provider for SummaryRepository."""
    return SummaryRepository(session)
