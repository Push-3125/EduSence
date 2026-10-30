"""Permission-Scoped Vector Retrieval Service with pgvector (US 2.14 / TASK-M4-05).

Adheres strictly to EduSense Invariants:
- Invariant 6 & 9: Retrieval ALWAYS filters by permission (user_id, document_scope).
  NEVER run unscoped vector queries. User A can NEVER retrieve chunks from User B's
  documents, even if specifically requesting User B's document IDs or course IDs.
- Invariant 4: Call embedding ONLY through shared `EmbeddingAdapter.aembed_text(...)`.
- US 2.14: Document-grounded retrieval. Answers must be grounded in student's documents.
  If nothing meets similarity threshold, flag as no information found.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends

from app.providers.embedding_adapter import EmbeddingAdapter, get_embedding_adapter
from app.repositories.chunk_repo import DocumentChunkRepository, get_chunk_repo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain Exceptions
# ---------------------------------------------------------------------------


class RetrievalError(Exception):
    """Base exception for vector retrieval operations."""


class EmptyQueryError(RetrievalError):
    """Raised when the retrieval query is empty or contains only whitespace."""


# ---------------------------------------------------------------------------
# Data Transfer Objects
# ---------------------------------------------------------------------------


@dataclass
class RetrievedChunk:
    """Decoupled DTO representing a retrieved document chunk with similarity score."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    chunk_index: int
    page_number: int | None
    content: str
    similarity_score: float


@dataclass
class RetrievalResult:
    """Consolidated DTO representing retrieval results with RAG context blocks."""

    chunks: list[RetrievedChunk]
    query: str
    total_found: int
    has_relevant_context: bool
    formatted_context: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def format_context_block(chunk: RetrievedChunk) -> str:
    """Format a single retrieved chunk into a standardized RAG context block.

    Preserves document title, page number, and chunk content.
    Format:
        [Document: {title}, Page {page_number}] {content}
        or
        [Document: {title}] {content} (if page_number is None)
    """
    if chunk.page_number is not None:
        header = f"[Document: {chunk.document_title}, Page {chunk.page_number}]"
    else:
        header = f"[Document: {chunk.document_title}]"
    return f"{header} {chunk.content}"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class RetrievalService:
    """Service orchestrating permission-scoped pgvector vector retrieval."""

    def __init__(
        self,
        chunk_repo: DocumentChunkRepository,
        embedding_adapter: EmbeddingAdapter,
    ) -> None:
        self.chunk_repo = chunk_repo
        self.embedding_adapter = embedding_adapter

    async def retrieve_context(
        self,
        user_id: uuid.UUID,
        query: str,
        document_ids: list[uuid.UUID] | None = None,
        course_id: uuid.UUID | None = None,
        top_k: int = 5,
        min_similarity: float = 0.5,
    ) -> RetrievalResult:
        """Perform permission-scoped semantic search and format context for RAG.

        Steps:
        1. Validates query is non-empty.
        2. Generates query vector via `embedding_adapter.aembed_text(query)` (768 dimensions).
        3. Queries `chunk_repo` with strict `user_id` filtering, and optional `document_ids` or `course_id` filters.
        4. Filters results by `similarity >= min_similarity`.
        5. Limits to `top_k`.
        6. Formats chunks into clean RAG context blocks:
           `[Document: {title}, Page {page_number}] {content}`.
        7. If no chunks found or none meet threshold: `has_relevant_context = False`, `formatted_context = ""`.

        Args:
            user_id: Authenticated user UUID for strict tenant isolation (Invariant 6 & 9).
            query: User search or study question.
            document_ids: Optional list of document UUIDs to scope retrieval.
            course_id: Optional course UUID to scope retrieval.
            top_k: Maximum number of chunks to return (default: 5).
            min_similarity: Minimum cosine similarity threshold [0.0, 1.0] (default: 0.5).

        Returns:
            RetrievalResult containing matched chunks, metadata, and formatted context.

        Raises:
            EmptyQueryError: If query is empty or whitespace-only.
            RetrievalError: If validation fails or downstream search errors occur.
        """
        # Step 1: Validate query is non-empty
        if query is None or not query.strip():
            raise EmptyQueryError("Query must not be empty or contain only whitespace.")
        cleaned_query = query.strip()

        if top_k < 1:
            raise RetrievalError("top_k must be at least 1.")
        if not (0.0 <= min_similarity <= 1.0):
            raise RetrievalError("min_similarity must be between 0.0 and 1.0.")

        # Step 2: Generate 768-dim query vector via shared EmbeddingAdapter (Invariant 4)
        try:
            query_vector = await self.embedding_adapter.aembed_text(cleaned_query)
        except Exception as exc:
            if isinstance(exc, RetrievalError):
                raise
            logger.error("Embedding generation failed for query: %s", exc)
            raise RetrievalError(f"Embedding generation failed: {exc}") from exc

        # Step 3: Query chunk_repo with strict user_id filtering (Invariant 6 & 9)
        try:
            raw_results = await self.chunk_repo.search_similar_chunks(
                user_id=user_id,
                query_embedding=query_vector,
                limit=top_k,
                min_similarity=min_similarity,
                course_id=course_id,
                document_ids=document_ids,
            )
        except Exception as exc:
            if isinstance(exc, RetrievalError):
                raise
            logger.error("Similarity search failed for user %s: %s", user_id, exc)
            raise RetrievalError(f"Vector search failed: {exc}") from exc

        # Step 4 & 5: Filter by min_similarity and limit to top_k
        filtered_chunks: list[RetrievedChunk] = []
        for chunk, score in raw_results:
            score_float = float(score)
            if score_float < min_similarity:
                continue

            # Safely determine document title
            doc_title = "Untitled Document"
            if hasattr(chunk, "document") and chunk.document is not None:
                doc_title = getattr(chunk.document, "title", "Untitled Document") or "Untitled Document"
            elif hasattr(chunk, "document_title") and chunk.document_title:
                doc_title = str(chunk.document_title)

            filtered_chunks.append(
                RetrievedChunk(
                    chunk_id=chunk.id,
                    document_id=chunk.document_id,
                    document_title=doc_title,
                    chunk_index=chunk.chunk_index,
                    page_number=chunk.page_number,
                    content=chunk.content,
                    similarity_score=score_float,
                )
            )

        final_chunks = filtered_chunks[:top_k]

        # Step 6 & 7: Format chunks into clean RAG context blocks or flag no context
        if not final_chunks:
            return RetrievalResult(
                chunks=[],
                query=cleaned_query,
                total_found=0,
                has_relevant_context=False,
                formatted_context="",
            )

        context_blocks = [format_context_block(c) for c in final_chunks]
        formatted_context = "\n\n".join(context_blocks)

        return RetrievalResult(
            chunks=final_chunks,
            query=cleaned_query,
            total_found=len(final_chunks),
            has_relevant_context=True,
            formatted_context=formatted_context,
        )


# ---------------------------------------------------------------------------
# Dependency Provider
# ---------------------------------------------------------------------------


def get_retrieval_service(
    chunk_repo: Annotated[DocumentChunkRepository, Depends(get_chunk_repo)],
    embedding_adapter: Annotated[EmbeddingAdapter, Depends(get_embedding_adapter)],
) -> RetrievalService:
    """FastAPI dependency provider for RetrievalService."""
    return RetrievalService(
        chunk_repo=chunk_repo,
        embedding_adapter=embedding_adapter,
    )


__all__ = [
    "EmptyQueryError",
    "RetrievalError",
    "RetrievalResult",
    "RetrievalService",
    "RetrievedChunk",
    "format_context_block",
    "get_retrieval_service",
]
