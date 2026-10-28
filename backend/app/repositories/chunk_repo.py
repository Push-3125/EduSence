"""Document chunk repository and SQLAlchemy model for pgvector storage (TASK-M3-05 / US 2.10).

Adheres strictly to EduSense Invariant 6:
"Retrieval always filters by permission (user_id, document_scope). Never run unscoped vector queries."
And Invariant 8:
"Every worker task passes through the Idempotency Guard before execution.
Chunks table has UNIQUE(document_id, chunk_index) with ON CONFLICT DO UPDATE."
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import (
    UUID,
)
from sqlalchemy.dialects.postgresql import (
    insert as pg_insert,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, contains_eager, mapped_column, relationship

from app.core.database import Base, get_db
from app.repositories.document_repo import Document


class DocumentChunk(Base):
    """DocumentChunk model representing document_chunks table with 768-dim pgvector embeddings."""

    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(768), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationship back to Document
    document: Mapped["Document"] = relationship("Document", backref="chunks", lazy="select")

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_index"),
        CheckConstraint("chunk_index >= 0", name="ck_document_chunks_index"),
        CheckConstraint("page_number IS NULL OR page_number > 0", name="ck_document_chunks_page"),
        CheckConstraint("token_count IS NULL OR token_count >= 0", name="ck_document_chunks_tokens"),
        CheckConstraint("length(btrim(content)) > 0", name="ck_document_chunks_content"),
    )


class DocumentChunkRepository:
    """Repository handling database operations and semantic similarity queries for document chunks."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_chunks(
        self,
        document_id: uuid.UUID,
        chunks_data: list[dict[str, Any]],
    ) -> list[DocumentChunk]:
        """Insert or UPSERT chunks for a document.

        Uses PostgreSQL ON CONFLICT (document_id, chunk_index) DO UPDATE
        to keep worker task retries idempotent per Invariant 8.

        Args:
            document_id: Owner document UUID.
            chunks_data: List of chunk dictionaries containing:
                - chunk_index: int
                - content: str
                - page_number: int | None
                - token_count: int | None
                - embedding: list[float]

        Returns:
            List of created or updated DocumentChunk instances.
        """
        if not chunks_data:
            return []

        records = [
            {
                "id": c.get("id") or uuid.uuid4(),
                "document_id": document_id,
                "chunk_index": c["chunk_index"],
                "content": c["content"],
                "page_number": c.get("page_number"),
                "token_count": c.get("token_count"),
                "embedding": c["embedding"],
            }
            for c in chunks_data
        ]

        insert_stmt = pg_insert(DocumentChunk).values(records)
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=["document_id", "chunk_index"],
            set_={
                "content": insert_stmt.excluded.content,
                "page_number": insert_stmt.excluded.page_number,
                "token_count": insert_stmt.excluded.token_count,
                "embedding": insert_stmt.excluded.embedding,
                "updated_at": func.now(),
            },
        ).returning(DocumentChunk)

        result = await self.session.execute(upsert_stmt)
        await self.session.flush()
        return list(result.scalars().all())

    async def get_chunks_by_document(self, document_id: uuid.UUID) -> list[DocumentChunk]:
        """Fetch all chunks belonging to a document ordered sequentially by chunk_index.

        Args:
            document_id: Document UUID.

        Returns:
            List of DocumentChunk instances in chunk_index ascending order.
        """
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def delete_chunks_by_document(self, document_id: uuid.UUID) -> int:
        """Delete all chunks belonging to a document.

        Args:
            document_id: Document UUID.

        Returns:
            Count of deleted chunk rows.
        """
        stmt = delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    async def search_similar_chunks(
        self,
        user_id: uuid.UUID,
        query_embedding: list[float],
        limit: int = 5,
        min_similarity: float = 0.0,
        course_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
        document_ids: list[uuid.UUID] | None = None,
    ) -> list[tuple[DocumentChunk, float]]:
        """Perform semantic similarity search on chunks with strict tenant isolation.

        Adheres strictly to Invariant 6 and Invariant 9:
        - Must join Document on Document.id == DocumentChunk.document_id and filter Document.user_id == user_id.
        - Never run unscoped vector queries.
        - Calculates cosine similarity = 1.0 - distance.
        - Orders results by similarity descending.

        Args:
            user_id: Authenticated student UUID (strict tenant isolation).
            query_embedding: 768-dimensional query vector.
            limit: Maximum chunks to return.
            min_similarity: Minimum cosine similarity threshold [0.0, 1.0].
            course_id: Optional course filter.
            document_id: Optional single document filter.
            document_ids: Optional list of document UUIDs to scope retrieval.

        Returns:
            List of tuples (DocumentChunk, similarity_score) sorted descending by similarity.
        """
        if document_ids is not None and len(document_ids) == 0:
            return []

        distance = DocumentChunk.embedding.cosine_distance(query_embedding)
        similarity = (1.0 - distance).label("similarity")

        stmt = (
            select(DocumentChunk, similarity)
            .join(Document, Document.id == DocumentChunk.document_id)
            .options(contains_eager(DocumentChunk.document))
            .where(Document.user_id == user_id)
        )

        if course_id is not None:
            stmt = stmt.where(Document.course_id == course_id)
        if document_id is not None:
            stmt = stmt.where(Document.id == document_id)
        if document_ids is not None:
            stmt = stmt.where(Document.id.in_(document_ids))

        if min_similarity > 0.0:
            stmt = stmt.where(similarity >= min_similarity)

        stmt = stmt.order_by(distance.asc()).limit(limit)

        result = await self.session.execute(stmt)
        rows = result.all()
        return [(row[0], float(row[1])) for row in rows]

    async def get_system_chunk_stats(self) -> dict[str, int]:
        """Aggregate total document chunks and total embedded chunks across the system.

        Returns:
            Dictionary containing:
            - total_chunks: int
            - embedded_chunks: int
        """
        stmt = select(
            func.count(DocumentChunk.id).label("total_chunks"),
            func.count(DocumentChunk.id)
            .filter(DocumentChunk.embedding.isnot(None))
            .label("embedded_chunks"),
        )
        res = await self.session.execute(stmt)
        row = res.one()
        return {
            "total_chunks": int(row.total_chunks or 0),
            "embedded_chunks": int(row.embedded_chunks or 0),
        }


def get_chunk_repo(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> DocumentChunkRepository:
    """FastAPI dependency provider for DocumentChunkRepository."""
    return DocumentChunkRepository(session)


__all__ = [
    "DocumentChunk",
    "DocumentChunkRepository",
    "get_chunk_repo",
]
