"""Unit and integration tests for Permission-Scoped Vector Retrieval Service (TASK-M4-05 / US 2.14).

Verifies:
- Invariant 6 & Invariant 9: Strict tenant isolation (retrieval always filters by permission:
  user_id, document_scope. User A can NEVER retrieve chunks from User B's documents,
  even if specifically requesting User B's document IDs or course IDs).
- Invariant 4: Call embedding ONLY through `embedding_adapter.aembed_text(...)`.
- US 2.14: Document-grounded retrieval. Answers must be grounded in student's documents.
  If nothing meets similarity threshold, flag as no information found (has_relevant_context=False).
- Context block formatting preserves document title, page number, and content.
- Query validation: empty or whitespace-only queries raise EmptyQueryError.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Ensure backend root is in sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest

from app.providers.embedding_adapter import EmbeddingAdapter, EmbeddingProviderError
from app.repositories.chunk_repo import DocumentChunk, DocumentChunkRepository
from app.repositories.document_repo import Document
from app.services.retrieval_service import (
    EmptyQueryError,
    RetrievalError,
    RetrievalResult,
    RetrievalService,
    RetrievedChunk,
    format_context_block,
    get_retrieval_service,
)

# ---------------------------------------------------------------------------
# Test Fixtures & In-Memory Fake Repositories
# ---------------------------------------------------------------------------


class FakeDocumentChunkRepo:
    """In-memory mock adhering to DocumentChunkRepository contract with tenant isolation."""

    def __init__(self) -> None:
        # Key: chunk_id -> (DocumentChunk, owner_user_id, course_id, doc_title)
        self.chunks: dict[uuid.UUID, tuple[DocumentChunk, uuid.UUID, uuid.UUID | None, str]] = {}

    def add_chunk(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        document_title: str,
        content: str,
        embedding: list[float],
        page_number: int | None = None,
        chunk_index: int = 0,
        course_id: uuid.UUID | None = None,
    ) -> DocumentChunk:
        chunk = DocumentChunk(
            id=uuid.uuid4(),
            document_id=document_id,
            chunk_index=chunk_index,
            content=content,
            page_number=page_number,
            embedding=embedding,
        )
        # Attach a mock Document object for eager relationship testing
        doc = Document(
            id=document_id,
            user_id=user_id,
            course_id=course_id,
            title=document_title,
            original_file_name="file.pdf",
            storage_object_key="key",
            file_type="application/pdf",
            file_size_bytes=1000,
        )
        chunk.document = doc
        self.chunks[chunk.id] = (chunk, user_id, course_id, document_title)
        return chunk

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
        """Strict tenant isolation: only return chunks where chunk's owner == user_id."""
        if document_ids is not None and len(document_ids) == 0:
            return []

        results: list[tuple[DocumentChunk, float]] = []
        q_norm = sum(x * x for x in query_embedding) ** 0.5

        for chunk, owner_id, doc_course_id, _title in self.chunks.values():
            # Invariant 6 & Invariant 9: Strict user isolation
            if owner_id != user_id:
                continue

            if document_id is not None and chunk.document_id != document_id:
                continue
            if document_ids is not None and chunk.document_id not in document_ids:
                continue
            if course_id is not None and doc_course_id != course_id:
                continue

            # Cosine similarity calculation
            c_norm = sum(x * x for x in chunk.embedding) ** 0.5
            if q_norm == 0 or c_norm == 0:
                sim = 0.0
            else:
                dot = sum(a * b for a, b in zip(query_embedding, chunk.embedding))
                sim = dot / (q_norm * c_norm)

            if sim >= min_similarity:
                results.append((chunk, sim))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]


@pytest.fixture
def fake_embedding_adapter() -> EmbeddingAdapter:
    """Mock EmbeddingAdapter returning predictable 768-dim embeddings."""
    adapter = MagicMock(spec=EmbeddingAdapter)

    async def mock_aembed_text(text: str) -> list[float]:
        # Return deterministic unit vector based on text
        if "calculus" in text.lower():
            return [1.0] + [0.0] * 767
        elif "history" in text.lower():
            return [0.0, 1.0] + [0.0] * 766
        return [0.5] * 768

    adapter.aembed_text = AsyncMock(side_effect=mock_aembed_text)
    return adapter


@pytest.fixture
def fake_chunk_repo() -> FakeDocumentChunkRepo:
    return FakeDocumentChunkRepo()


@pytest.fixture
def retrieval_service(
    fake_chunk_repo: FakeDocumentChunkRepo,
    fake_embedding_adapter: EmbeddingAdapter,
) -> RetrievalService:
    return RetrievalService(
        chunk_repo=fake_chunk_repo,  # type: ignore[arg-type]
        embedding_adapter=fake_embedding_adapter,
    )


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_query_raises_empty_query_error(
    retrieval_service: RetrievalService,
) -> None:
    """Verifies that empty or whitespace-only queries raise EmptyQueryError."""
    user_id = uuid.uuid4()

    with pytest.raises(EmptyQueryError) as exc_info1:
        await retrieval_service.retrieve_context(user_id=user_id, query="")
    assert "empty" in str(exc_info1.value).lower()

    with pytest.raises(EmptyQueryError) as exc_info2:
        await retrieval_service.retrieve_context(user_id=user_id, query="   \t \n ")
    assert "whitespace" in str(exc_info2.value).lower()


@pytest.mark.asyncio
async def test_invalid_parameters_raise_retrieval_error(
    retrieval_service: RetrievalService,
) -> None:
    """Verifies that invalid top_k or min_similarity raise RetrievalError."""
    user_id = uuid.uuid4()

    with pytest.raises(RetrievalError) as exc1:
        await retrieval_service.retrieve_context(user_id=user_id, query="test", top_k=0)
    assert "top_k" in str(exc1.value)

    with pytest.raises(RetrievalError) as exc2:
        await retrieval_service.retrieve_context(user_id=user_id, query="test", min_similarity=-0.1)
    assert "min_similarity" in str(exc2.value)

    with pytest.raises(RetrievalError) as exc3:
        await retrieval_service.retrieve_context(user_id=user_id, query="test", min_similarity=1.5)
    assert "min_similarity" in str(exc3.value)


@pytest.mark.asyncio
async def test_scoped_retrieval_success_authorized_user(
    retrieval_service: RetrievalService,
    fake_chunk_repo: FakeDocumentChunkRepo,
    fake_embedding_adapter: EmbeddingAdapter,
) -> None:
    """Verifies successful vector retrieval for authorized user and context construction."""
    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()

    # Add chunk matching calculus query ([1.0] + [0.0] * 767)
    fake_chunk_repo.add_chunk(
        user_id=user_id,
        document_id=doc_id,
        document_title="Calculus Fundamentals",
        content="Derivatives represent instantaneous rate of change.",
        embedding=[1.0] + [0.0] * 767,
        page_number=12,
        chunk_index=0,
    )
    fake_chunk_repo.add_chunk(
        user_id=user_id,
        document_id=doc_id,
        document_title="Calculus Fundamentals",
        content="Integrals compute the area under the curve.",
        embedding=[0.8, 0.6] + [0.0] * 766,
        page_number=14,
        chunk_index=1,
    )

    result = await retrieval_service.retrieve_context(
        user_id=user_id,
        query="Explain calculus derivatives and integrals",
        min_similarity=0.7,
        top_k=5,
    )

    assert isinstance(result, RetrievalResult)
    assert result.has_relevant_context is True
    assert result.total_found == 2
    assert len(result.chunks) == 2
    assert result.query == "Explain calculus derivatives and integrals"

    # Verify RetrievedChunk attributes
    first_chunk = result.chunks[0]
    assert isinstance(first_chunk, RetrievedChunk)
    assert first_chunk.document_id == doc_id
    assert first_chunk.document_title == "Calculus Fundamentals"
    assert first_chunk.page_number == 12
    assert "Derivatives represent" in first_chunk.content
    assert first_chunk.similarity_score >= 0.7

    # Verify formatted context blocks
    assert "[Document: Calculus Fundamentals, Page 12] Derivatives represent" in result.formatted_context
    assert "[Document: Calculus Fundamentals, Page 14] Integrals compute" in result.formatted_context


@pytest.mark.asyncio
async def test_strict_tenant_isolation_user_b_chunks_never_leaked(
    retrieval_service: RetrievalService,
    fake_chunk_repo: FakeDocumentChunkRepo,
) -> None:
    """Invariant 6 & Invariant 9: User A NEVER retrieves User B's chunks.

    Even if User B has a chunk with 1.0 perfect match, and User A passes User B's document_id.
    """
    user_a_id = uuid.uuid4()
    user_b_id = uuid.uuid4()

    doc_a_id = uuid.uuid4()
    doc_b_id = uuid.uuid4()

    # User A has a calculus chunk with 0.8 similarity
    fake_chunk_repo.add_chunk(
        user_id=user_a_id,
        document_id=doc_a_id,
        document_title="User A Notes",
        content="User A public calculus notes.",
        embedding=[0.8, 0.6] + [0.0] * 766,
        page_number=1,
    )

    # User B has a CONFIDENTIAL calculus chunk with 1.0 similarity
    fake_chunk_repo.add_chunk(
        user_id=user_b_id,
        document_id=doc_b_id,
        document_title="User B Secret Exam",
        content="CONFIDENTIAL: User B upcoming midterm answers.",
        embedding=[1.0] + [0.0] * 767,
        page_number=5,
    )

    # User A queries for calculus
    result_a = await retrieval_service.retrieve_context(
        user_id=user_a_id,
        query="calculus exam questions",
        min_similarity=0.5,
    )

    # User A must ONLY see User A's chunk
    assert result_a.total_found == 1
    assert result_a.chunks[0].document_id == doc_a_id
    assert "CONFIDENTIAL" not in result_a.formatted_context

    # Even if User A maliciously attempts to filter by User B's document_id:
    result_malicious = await retrieval_service.retrieve_context(
        user_id=user_a_id,
        query="calculus exam questions",
        document_ids=[doc_b_id],
        min_similarity=0.5,
    )
    assert result_malicious.total_found == 0
    assert result_malicious.has_relevant_context is False
    assert result_malicious.formatted_context == ""


@pytest.mark.asyncio
async def test_filter_by_specific_document_ids(
    retrieval_service: RetrievalService,
    fake_chunk_repo: FakeDocumentChunkRepo,
) -> None:
    """Verifies scoping retrieval to a specific list of document_ids."""
    user_id = uuid.uuid4()
    doc1_id = uuid.uuid4()
    doc2_id = uuid.uuid4()

    fake_chunk_repo.add_chunk(
        user_id=user_id,
        document_id=doc1_id,
        document_title="Doc 1",
        content="Calculus Chapter 1 content.",
        embedding=[1.0] + [0.0] * 767,
        page_number=1,
    )
    fake_chunk_repo.add_chunk(
        user_id=user_id,
        document_id=doc2_id,
        document_title="Doc 2",
        content="Calculus Chapter 2 content.",
        embedding=[1.0] + [0.0] * 767,
        page_number=2,
    )

    # Filter to only doc1
    result = await retrieval_service.retrieve_context(
        user_id=user_id,
        query="calculus",
        document_ids=[doc1_id],
    )

    assert result.total_found == 1
    assert result.chunks[0].document_id == doc1_id
    assert "Doc 1" in result.formatted_context
    assert "Doc 2" not in result.formatted_context


@pytest.mark.asyncio
async def test_filter_by_course_id(
    retrieval_service: RetrievalService,
    fake_chunk_repo: FakeDocumentChunkRepo,
) -> None:
    """Verifies scoping retrieval to a specific course_id."""
    user_id = uuid.uuid4()
    course_math_id = uuid.uuid4()
    course_physics_id = uuid.uuid4()

    fake_chunk_repo.add_chunk(
        user_id=user_id,
        document_id=uuid.uuid4(),
        document_title="Math Syllabus",
        content="Calculus limits and continuity.",
        embedding=[1.0] + [0.0] * 767,
        course_id=course_math_id,
    )
    fake_chunk_repo.add_chunk(
        user_id=user_id,
        document_id=uuid.uuid4(),
        document_title="Physics Syllabus",
        content="Kinematics and calculus applications.",
        embedding=[1.0] + [0.0] * 767,
        course_id=course_physics_id,
    )

    result = await retrieval_service.retrieve_context(
        user_id=user_id,
        query="calculus",
        course_id=course_math_id,
    )

    assert result.total_found == 1
    assert result.chunks[0].document_title == "Math Syllabus"
    assert "Physics Syllabus" not in result.formatted_context


@pytest.mark.asyncio
async def test_empty_document_ids_list_returns_no_context(
    retrieval_service: RetrievalService,
    fake_chunk_repo: FakeDocumentChunkRepo,
) -> None:
    """Passing an empty list of document_ids returns empty context safely without error."""
    user_id = uuid.uuid4()
    fake_chunk_repo.add_chunk(
        user_id=user_id,
        document_id=uuid.uuid4(),
        document_title="Doc",
        content="Some content",
        embedding=[1.0] + [0.0] * 767,
    )

    result = await retrieval_service.retrieve_context(
        user_id=user_id,
        query="calculus",
        document_ids=[],
    )

    assert result.total_found == 0
    assert result.has_relevant_context is False
    assert result.formatted_context == ""


@pytest.mark.asyncio
async def test_min_similarity_threshold_filters_unrelated_chunks(
    retrieval_service: RetrievalService,
    fake_chunk_repo: FakeDocumentChunkRepo,
) -> None:
    """Chunks below min_similarity are discarded."""
    user_id = uuid.uuid4()

    # Chunk with orthogonal / 0.0 similarity to calculus
    fake_chunk_repo.add_chunk(
        user_id=user_id,
        document_id=uuid.uuid4(),
        document_title="Low Similarity Doc",
        content="Ancient Greek history notes.",
        embedding=[0.0, 1.0] + [0.0] * 766,  # Cosine similarity == 0.0 with query vector
    )

    result = await retrieval_service.retrieve_context(
        user_id=user_id,
        query="calculus integrals",
        min_similarity=0.5,
    )

    assert result.total_found == 0
    assert result.has_relevant_context is False
    assert result.formatted_context == ""
    assert result.chunks == []


@pytest.mark.asyncio
async def test_empty_retrieval_handling_when_no_documents_exist(
    retrieval_service: RetrievalService,
) -> None:
    """US 2.14: When user has no documents or no matches found, flag as no information."""
    user_id = uuid.uuid4()

    result = await retrieval_service.retrieve_context(
        user_id=user_id,
        query="Quantum physics",
        min_similarity=0.5,
    )

    assert result.has_relevant_context is False
    assert result.formatted_context == ""
    assert result.total_found == 0
    assert result.chunks == []
    assert result.query == "Quantum physics"


@pytest.mark.asyncio
async def test_context_block_formatting_with_and_without_page_number() -> None:
    """Verifies RAG context block formatting preserves title, page, and content."""
    chunk_with_page = RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title="Machine Learning",
        chunk_index=2,
        page_number=45,
        content="Backpropagation computes gradients using the chain rule.",
        similarity_score=0.92,
    )
    block1 = format_context_block(chunk_with_page)
    assert block1 == "[Document: Machine Learning, Page 45] Backpropagation computes gradients using the chain rule."

    chunk_without_page = RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title="Notes Summary",
        chunk_index=0,
        page_number=None,
        content="Summary of key formulas.",
        similarity_score=0.88,
    )
    block2 = format_context_block(chunk_without_page)
    assert block2 == "[Document: Notes Summary] Summary of key formulas."


@pytest.mark.asyncio
async def test_top_k_limits_maximum_results(
    retrieval_service: RetrievalService,
    fake_chunk_repo: FakeDocumentChunkRepo,
) -> None:
    """Verifies that top_k restricts the number of returned chunks."""
    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()

    for i in range(8):
        fake_chunk_repo.add_chunk(
            user_id=user_id,
            document_id=doc_id,
            document_title="Calculus Guide",
            content=f"Chapter {i} concepts.",
            embedding=[1.0] + [0.0] * 767,
            page_number=i + 1,
            chunk_index=i,
        )

    result = await retrieval_service.retrieve_context(
        user_id=user_id,
        query="calculus",
        top_k=3,
        min_similarity=0.5,
    )

    assert result.total_found == 3
    assert len(result.chunks) == 3


@pytest.mark.asyncio
async def test_embedding_adapter_failure_raises_retrieval_error(
    fake_chunk_repo: FakeDocumentChunkRepo,
) -> None:
    """Downstream embedding failures are wrapped in RetrievalError."""
    failing_adapter = MagicMock(spec=EmbeddingAdapter)
    failing_adapter.aembed_text = AsyncMock(
        side_effect=EmbeddingProviderError("Gemini quota exceeded or network timeout")
    )

    service = RetrievalService(
        chunk_repo=fake_chunk_repo,  # type: ignore[arg-type]
        embedding_adapter=failing_adapter,
    )

    with pytest.raises(RetrievalError) as exc_info:
        await service.retrieve_context(user_id=uuid.uuid4(), query="test query")

    assert "Embedding generation failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_database_vector_search_failure_raises_retrieval_error(
    fake_embedding_adapter: EmbeddingAdapter,
) -> None:
    """Downstream repository failures are caught and wrapped in RetrievalError."""
    failing_repo = MagicMock(spec=DocumentChunkRepository)
    failing_repo.search_similar_chunks = AsyncMock(side_effect=RuntimeError("DB connection lost"))

    service = RetrievalService(
        chunk_repo=failing_repo,
        embedding_adapter=fake_embedding_adapter,
    )

    with pytest.raises(RetrievalError) as exc_info:
        await service.retrieve_context(user_id=uuid.uuid4(), query="calculus")

    assert "Vector search failed" in str(exc_info.value)


def test_get_retrieval_service_dependency_provider() -> None:
    """FastAPI dependency provider returns a properly configured RetrievalService."""
    mock_repo = MagicMock(spec=DocumentChunkRepository)
    mock_adapter = MagicMock(spec=EmbeddingAdapter)

    service = get_retrieval_service(
        chunk_repo=mock_repo,
        embedding_adapter=mock_adapter,
    )

    assert isinstance(service, RetrievalService)
    assert service.chunk_repo is mock_repo
    assert service.embedding_adapter is mock_adapter
