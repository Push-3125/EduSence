"""Citation repository and SQLAlchemy model for RAG grounding and citation verification (US 2.13 / US 2.14 / TASK-M4-07).

Adheres strictly to EduSense Invariants:
- Invariant 5: Retrieval is separate from Generation: every LLM answer must pass GroundingChecker before being returned.
  Citations must come ONLY from retrieved chunks; never fabricate sources.
- Invariant 6: Strict tenant isolation. Citations link assistant messages directly to authorized document chunks.
- Invariant 2: Repository layer isolated from presentation and routers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, joinedload, mapped_column, relationship

from app.core.database import Base, get_db
from app.repositories.chat_repo import AIMessage
from app.repositories.chunk_repo import DocumentChunk


class AnswerCitation(Base):
    """Answer citation model representing the answer_citations table.

    Maps an assistant message to the specific source document chunks used as evidence.
    """

    __tablename__ = "answer_citations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_chunks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    citation_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    quoted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    message: Mapped[AIMessage] = relationship("AIMessage", lazy="select")
    chunk: Mapped[DocumentChunk] = relationship("DocumentChunk", lazy="joined")

    __table_args__ = (
        UniqueConstraint("message_id", "chunk_id", name="uq_answer_citations_message_chunk"),
        UniqueConstraint("message_id", "citation_order", name="uq_answer_citations_order"),
        CheckConstraint("citation_order > 0", name="ck_answer_citations_order"),
        CheckConstraint(
            "relevance_score IS NULL OR relevance_score BETWEEN 0 AND 1",
            name="ck_answer_citations_score",
        ),
        Index("idx_answer_citations_chunk_id", "chunk_id"),
    )


class CitationRepository:
    """Repository handling database operations for answer citations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_citations(
        self,
        message_id: uuid.UUID,
        citations_data: list[dict[str, Any]],
    ) -> list[AnswerCitation]:
        """Insert sequential citations for an AI assistant message.

        Args:
            message_id: AI message UUID.
            citations_data: List of citation dictionaries containing:
                - chunk_id: uuid.UUID
                - citation_order: int | None (optional, assigned sequentially if None/0)
                - relevance_score: float | None (clamped to [0.0, 1.0])
                - quoted_text: str | None

        Returns:
            List of created AnswerCitation instances.
        """
        if not citations_data:
            return []

        # Deduplicate by chunk_id to satisfy uq_answer_citations_message_chunk
        seen_chunks: set[uuid.UUID] = set()
        deduped_data: list[dict[str, Any]] = []
        for item in citations_data:
            cid = item["chunk_id"]
            if isinstance(cid, str):
                cid = uuid.UUID(cid)
            if cid not in seen_chunks:
                seen_chunks.add(cid)
                deduped_data.append({**item, "chunk_id": cid})

        created: list[AnswerCitation] = []
        for idx, item in enumerate(deduped_data, start=1):
            order = item.get("citation_order")
            if order is None or order <= 0:
                order = idx

            score = item.get("relevance_score")
            if score is not None:
                score = round(max(0.0, min(1.0, float(score))), 4)

            quoted = item.get("quoted_text")
            if quoted is not None:
                quoted = str(quoted).strip() or None

            citation = AnswerCitation(
                id=item.get("id") or uuid.uuid4(),
                message_id=message_id,
                chunk_id=item["chunk_id"],
                citation_order=order,
                relevance_score=score,
                quoted_text=quoted,
            )
            self.session.add(citation)
            created.append(citation)

        await self.session.flush()
        return created

    async def get_citations_by_message(
        self,
        message_id: uuid.UUID,
    ) -> list[AnswerCitation]:
        """Fetch all citations for a message ordered by citation_order ASC with chunk metadata.

        Args:
            message_id: AI message UUID.

        Returns:
            List of AnswerCitation instances in citation_order ascending order.
        """
        stmt = (
            select(AnswerCitation)
            .options(
                joinedload(AnswerCitation.chunk).joinedload(DocumentChunk.document),
            )
            .where(AnswerCitation.message_id == message_id)
            .order_by(AnswerCitation.citation_order.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def delete_citations_by_message(
        self,
        message_id: uuid.UUID,
    ) -> int:
        """Delete all citations associated with a message.

        Args:
            message_id: AI message UUID.

        Returns:
            Count of deleted citation rows.
        """
        stmt = delete(AnswerCitation).where(AnswerCitation.message_id == message_id)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return int(getattr(result, "rowcount", 0) or 0)


def get_citation_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> CitationRepository:
    """FastAPI dependency provider for CitationRepository."""
    return CitationRepository(session=session)


__all__ = [
    "AnswerCitation",
    "CitationRepository",
    "get_citation_repo",
]
