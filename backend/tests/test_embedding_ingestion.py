"""Comprehensive tests for EmbeddingAdapter, DocumentChunkRepository, and Document Ingestion Pipeline (TASK-M3-05).

Validates:
- EduSense Invariant 4: Shared EmbeddingAdapter provider with 768-dim pgvector vectors.
- EduSense Invariant 6: Strict tenant isolation during vector retrieval.
- EduSense Invariant 8: Chunks stored with UNIQUE(document_id, chunk_index) and ON CONFLICT DO UPDATE.
- Database constraint: ck_documents_embedded_complete (status <> 'embedded' OR (total_chunks > 0 AND chunks_processed = total_chunks)).
"""

import io
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import docx
import pytest
from google.genai import types
from pypdf import PdfWriter

# Ensure backend root is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.clients.storage_client import (
    FileNotFoundStorageError,
    MinIOStorageClient,
)
from app.providers.embedding_adapter import (
    EmbeddingAdapter,
    EmbeddingConfigurationError,
    EmbeddingProviderError,
)
from app.repositories.chunk_repo import (
    DocumentChunk,
    DocumentChunkRepository,
)
from app.repositories.course_repo import CourseRepository
from app.repositories.document_repo import Document, DocumentRepository
from app.services.chunking_service import get_chunking_service
from app.services.document_service import (
    DocumentNotFoundError,
    DocumentService,
)
from app.services.extraction_service import (
    EmptyDocumentError,
    get_extraction_service,
)
from app.worker.tasks.document_tasks import ingest_document_task

# ---------------------------------------------------------------------------
# Test Artifact Helpers
# ---------------------------------------------------------------------------


def _create_pdf_bytes(text: str = "EduSense Sample Content", num_pages: int = 1) -> bytes:
    """Generate in-memory PDF with valid text stream."""
    sanitized_text = text.replace("(", "").replace(")", "")
    stream_content = f"BT /F1 12 Tf 50 700 Td ({sanitized_text}) Tj ET".encode("latin1")
    single_page_pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        b"4 0 obj << /Length " + str(len(stream_content)).encode() + b" >> stream\n"
        + stream_content + b"\nendstream endobj\n"
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n0\n%%EOF"
    )

    if num_pages == 1:
        return single_page_pdf

    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(single_page_pdf))
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_page(reader.pages[0])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _create_blank_pdf_bytes(num_pages: int = 1) -> bytes:
    """Generate in-memory blank PDF with 0 extractable text."""
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _create_docx_bytes(paragraphs: list[str]) -> bytes:
    """Generate in-memory valid DOCX package."""
    doc = docx.Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Test Doubles
# ---------------------------------------------------------------------------


class FakeStorageClient(MinIOStorageClient):
    """In-memory storage client for document tests."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def upload_file(
        self,
        object_name: str,
        file_data: bytes | Any,
        length: int | None = None,
        content_type: str = "application/octet-stream",
        bucket_name: str | None = None,
    ) -> str:
        if isinstance(file_data, bytes):
            self.files[object_name] = file_data
        else:
            self.files[object_name] = file_data.read()
        return object_name

    def download_file(self, object_name: str, bucket_name: str | None = None) -> bytes:
        if object_name not in self.files:
            raise FileNotFoundStorageError(f"Object '{object_name}' not found.")
        return self.files[object_name]

    def delete_file(self, object_name: str, bucket_name: str | None = None) -> bool:
        existed = object_name in self.files
        self.files.pop(object_name, None)
        return existed


class FakeDocumentRepository(DocumentRepository):
    """In-memory DocumentRepository double."""

    def __init__(self) -> None:
        self.documents: dict[uuid.UUID, Document] = {}

    async def create(
        self,
        user_id: uuid.UUID,
        title: str,
        original_file_name: str,
        storage_object_key: str,
        file_type: str,
        file_size_bytes: int,
        course_id: uuid.UUID | None = None,
        page_count: int | None = None,
        status: str = "pending",
        id: uuid.UUID | None = None,
    ) -> Document:
        doc = Document(
            id=id or uuid.uuid4(),
            user_id=user_id,
            course_id=course_id,
            title=title,
            original_file_name=original_file_name,
            storage_object_key=storage_object_key,
            file_type=file_type,
            file_size_bytes=file_size_bytes,
            page_count=page_count,
            status=status,
            total_chunks=0,
            chunks_processed=0,
            processing_error_code=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.documents[doc.id] = doc
        return doc

    async def get_by_id_and_user(
        self,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Document | None:
        doc = self.documents.get(document_id)
        if doc and doc.user_id == user_id:
            return doc
        return None

    async def update(self, document: Document, **kwargs: Any) -> Document:
        for k, v in kwargs.items():
            setattr(document, k, v)
        document.updated_at = datetime.now(UTC)
        self.documents[document.id] = document
        return document

    async def delete(self, document: Document) -> None:
        self.documents.pop(document.id, None)


class FakeDocumentChunkRepository(DocumentChunkRepository):
    """In-memory DocumentChunkRepository double with pgvector cosine similarity."""

    def __init__(self, document_repo: FakeDocumentRepository | None = None) -> None:
        self.chunks: dict[uuid.UUID, DocumentChunk] = {}
        self.doc_repo = document_repo

    async def create_chunks(
        self,
        document_id: uuid.UUID,
        chunks_data: list[dict[str, Any]],
    ) -> list[DocumentChunk]:
        created: list[DocumentChunk] = []
        for c in chunks_data:
            chunk_idx = c["chunk_index"]

            # UPSERT simulation on (document_id, chunk_index)
            existing = None
            for ch in self.chunks.values():
                if ch.document_id == document_id and ch.chunk_index == chunk_idx:
                    existing = ch
                    break

            if existing:
                existing.content = c["content"]
                existing.page_number = c.get("page_number")
                existing.token_count = c.get("token_count")
                existing.embedding = list(c["embedding"])
                existing.updated_at = datetime.now(UTC)
                created.append(existing)
            else:
                chunk_id = c.get("id") or uuid.uuid4()
                chunk = DocumentChunk(
                    id=chunk_id,
                    document_id=document_id,
                    chunk_index=chunk_idx,
                    content=c["content"],
                    page_number=c.get("page_number"),
                    token_count=c.get("token_count"),
                    embedding=list(c["embedding"]),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                self.chunks[chunk_id] = chunk
                created.append(chunk)

        return created

    async def get_chunks_by_document(self, document_id: uuid.UUID) -> list[DocumentChunk]:
        return sorted(
            [c for c in self.chunks.values() if c.document_id == document_id],
            key=lambda c: c.chunk_index,
        )

    async def delete_chunks_by_document(self, document_id: uuid.UUID) -> int:
        to_del = [cid for cid, c in self.chunks.items() if c.document_id == document_id]
        for cid in to_del:
            del self.chunks[cid]
        return len(to_del)

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
        # Strict tenant isolation
        results: list[tuple[DocumentChunk, float]] = []

        if document_ids is not None and len(document_ids) == 0:
            return []

        q_norm = sum(x * x for x in query_embedding) ** 0.5

        for chunk in self.chunks.values():
            if document_id is not None and chunk.document_id != document_id:
                continue
            if document_ids is not None and chunk.document_id not in document_ids:
                continue

            # Verify document belongs to user_id
            if self.doc_repo:
                doc = await self.doc_repo.get_by_id_and_user(chunk.document_id, user_id)
                if not doc:
                    continue
                if course_id is not None and doc.course_id != course_id:
                    continue

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


# ---------------------------------------------------------------------------
# Part 1: EmbeddingAdapter Unit Tests
# ---------------------------------------------------------------------------


def test_embedding_adapter_init_missing_key_raises_error() -> None:
    """Verifies that initializing without API key and not in test mode raises EmbeddingConfigurationError."""
    with patch("app.providers.embedding_adapter.settings.gemini_api_key", None):
        with pytest.raises(EmbeddingConfigurationError) as exc_info:
            EmbeddingAdapter(api_key=None, test_mode=False)
        assert "GEMINI_API_KEY" in str(exc_info.value)


def test_embedding_adapter_init_with_key() -> None:
    """Verifies that passing a valid API key initializes the adapter."""
    adapter = EmbeddingAdapter(api_key="test-api-key", test_mode=False)
    assert adapter.dimension == 768
    assert adapter.model == "gemini-embedding-2"


def test_embedding_adapter_test_mode_generates_768_dim_vectors() -> None:
    """Verifies that test mode returns deterministic, unit-normalized 768-dim vectors."""
    adapter = EmbeddingAdapter(test_mode=True)
    v1 = adapter.embed_text("University Calculus Notes")
    v2 = adapter.embed_text("University Calculus Notes")
    v3 = adapter.embed_text("Modern Operating Systems")

    assert len(v1) == 768
    assert len(v2) == 768
    assert v1 == v2  # Deterministic for same text
    assert v1 != v3  # Distinct for different text

    # Check vector is approximately unit-normalized
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-4


def test_embedding_adapter_embed_text_with_mock_client() -> None:
    """Verifies embed_text using a mocked genai client."""
    mock_client = MagicMock()
    mock_vector = [0.05] * 768
    mock_response = types.EmbedContentResponse(
        embeddings=[types.ContentEmbedding(values=mock_vector)]
    )
    mock_client.models.embed_content.return_value = mock_response

    adapter = EmbeddingAdapter(client=mock_client)
    res = adapter.embed_text("Machine learning fundamentals")

    assert len(res) == 768
    assert res == mock_vector
    mock_client.models.embed_content.assert_called_once()


def test_embedding_adapter_embed_batch_splits_large_batches() -> None:
    """Verifies embed_batch splits batches according to batch_size."""
    mock_client = MagicMock()
    mock_vector = [0.01] * 768

    def _mock_call(*args: Any, **kwargs: Any) -> types.EmbedContentResponse:
        contents = kwargs.get("contents", [])
        return types.EmbedContentResponse(
            embeddings=[types.ContentEmbedding(values=mock_vector) for _ in contents]
        )

    mock_client.models.embed_content.side_effect = _mock_call

    # Batch size of 2, with 5 texts -> should call embed_content 3 times
    adapter = EmbeddingAdapter(client=mock_client, batch_size=2)
    texts = ["chunk 1", "chunk 2", "chunk 3", "chunk 4", "chunk 5"]
    embeddings = adapter.embed_batch(texts)

    assert len(embeddings) == 5
    assert mock_client.models.embed_content.call_count == 3


@pytest.mark.asyncio
async def test_embedding_adapter_async_aembed_text_and_aembed_batch() -> None:
    """Verifies asynchronous embedding methods with aio client."""
    mock_client = MagicMock()
    mock_vector = [0.02] * 768

    def _mock_aio_call(*args: Any, **kwargs: Any) -> types.EmbedContentResponse:
        contents = kwargs.get("contents", [])
        if isinstance(contents, str):
            contents = [contents]
        return types.EmbedContentResponse(
            embeddings=[types.ContentEmbedding(values=mock_vector) for _ in contents]
        )

    mock_aio_models = MagicMock()
    mock_aio_models.embed_content = AsyncMock(side_effect=_mock_aio_call)
    mock_client.aio.models = mock_aio_models

    adapter = EmbeddingAdapter(client=mock_client)
    res = await adapter.aembed_text("Async text embedding")

    assert len(res) == 768
    assert res == mock_vector
    mock_aio_models.embed_content.assert_called_once()

    # Async batch
    batch_res = await adapter.aembed_batch(["Text A", "Text B"])
    assert len(batch_res) == 2


def test_embedding_adapter_dimension_mismatch_raises_error() -> None:
    """Verifies that returning unexpected vector dimensions raises EmbeddingProviderError."""
    mock_client = MagicMock()
    # Return 512 dimensions instead of 768
    wrong_dim_vector = [0.1] * 512
    mock_response = types.EmbedContentResponse(
        embeddings=[types.ContentEmbedding(values=wrong_dim_vector)]
    )
    mock_client.models.embed_content.return_value = mock_response

    adapter = EmbeddingAdapter(client=mock_client, dimension=768)
    with pytest.raises(EmbeddingProviderError) as exc_info:
        adapter.embed_text("Test mismatch")
    assert "dimension 512, expected 768" in str(exc_info.value)


def test_embedding_adapter_api_failure_wrapped_in_provider_error() -> None:
    """Verifies that API exceptions are wrapped in EmbeddingProviderError."""
    mock_client = MagicMock()
    mock_client.models.embed_content.side_effect = RuntimeError("Quota exceeded or network timeout")

    adapter = EmbeddingAdapter(client=mock_client)
    with pytest.raises(EmbeddingProviderError) as exc_info:
        adapter.embed_text("Failing request")
    assert "Quota exceeded" in str(exc_info.value)


def test_embedding_adapter_empty_batch_returns_empty_list() -> None:
    """Verifies passing empty list returns empty list immediately."""
    adapter = EmbeddingAdapter(test_mode=True)
    assert adapter.embed_batch([]) == []


# ---------------------------------------------------------------------------
# Part 2: DocumentChunkRepository Tests & Tenant Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_repo_create_chunks() -> None:
    """Verifies chunk creation and retrieval."""
    doc_id = uuid.uuid4()
    repo = FakeDocumentChunkRepository()

    chunks_data = [
        {
            "chunk_index": 0,
            "content": "First chunk content",
            "page_number": 1,
            "token_count": 50,
            "embedding": [0.1] * 768,
        },
        {
            "chunk_index": 1,
            "content": "Second chunk content",
            "page_number": 2,
            "token_count": 60,
            "embedding": [0.2] * 768,
        },
    ]

    created = await repo.create_chunks(doc_id, chunks_data)
    assert len(created) == 2
    assert created[0].chunk_index == 0
    assert created[1].chunk_index == 1

    fetched = await repo.get_chunks_by_document(doc_id)
    assert len(fetched) == 2
    assert fetched[0].content == "First chunk content"
    assert fetched[1].content == "Second chunk content"


@pytest.mark.asyncio
async def test_chunk_repo_upsert_duplicate_chunk_index() -> None:
    """Verifies UPSERT behavior on (document_id, chunk_index) per Invariant 8."""
    doc_id = uuid.uuid4()
    repo = FakeDocumentChunkRepository()

    # Initial chunk
    await repo.create_chunks(
        doc_id,
        [
            {
                "chunk_index": 0,
                "content": "Original chunk content",
                "page_number": 1,
                "token_count": 40,
                "embedding": [0.1] * 768,
            }
        ],
    )

    # Re-run ingestion retry with updated content
    updated = await repo.create_chunks(
        doc_id,
        [
            {
                "chunk_index": 0,
                "content": "Updated chunk content after retry",
                "page_number": 1,
                "token_count": 45,
                "embedding": [0.9] * 768,
            }
        ],
    )

    assert len(updated) == 1
    assert updated[0].content == "Updated chunk content after retry"
    assert updated[0].embedding == [0.9] * 768

    # Ensure no duplicate rows were created
    all_chunks = await repo.get_chunks_by_document(doc_id)
    assert len(all_chunks) == 1


@pytest.mark.asyncio
async def test_chunk_repo_delete_chunks_by_document() -> None:
    """Verifies deleting chunks for a document."""
    doc_id = uuid.uuid4()
    repo = FakeDocumentChunkRepository()

    await repo.create_chunks(
        doc_id,
        [{"chunk_index": 0, "content": "Text", "page_number": 1, "token_count": 10, "embedding": [0.1] * 768}],
    )
    count = await repo.delete_chunks_by_document(doc_id)
    assert count == 1
    assert await repo.get_chunks_by_document(doc_id) == []


@pytest.mark.asyncio
async def test_chunk_repo_strict_tenant_isolation_search() -> None:
    """CRITICAL TEST (Invariant 6):

    Verifies semantic search strictly isolates tenant data by user_id.
    Student A must NEVER retrieve Student B's chunks, even if Student B's chunks
    have a 100% exact vector match!
    """
    doc_repo = FakeDocumentRepository()
    chunk_repo = FakeDocumentChunkRepository(document_repo=doc_repo)

    student_a_id = uuid.uuid4()
    student_b_id = uuid.uuid4()
    course_a_id = uuid.uuid4()

    # Create document for Student A
    doc_a = await doc_repo.create(
        user_id=student_a_id,
        title="Student A Document",
        original_file_name="a.pdf",
        storage_object_key="key_a",
        file_type="application/pdf",
        file_size_bytes=1000,
        course_id=course_a_id,
    )

    # Create document for Student B
    doc_b = await doc_repo.create(
        user_id=student_b_id,
        title="Student B Confidential Exam",
        original_file_name="b.pdf",
        storage_object_key="key_b",
        file_type="application/pdf",
        file_size_bytes=1000,
    )

    query_vector = [1.0] + [0.0] * 767

    # Student A has chunk with medium similarity
    await chunk_repo.create_chunks(
        doc_a.id,
        [
            {
                "chunk_index": 0,
                "content": "Student A study chunk",
                "page_number": 1,
                "token_count": 30,
                "embedding": [0.5] * 768,
            }
        ],
    )

    # Student B has chunk with PERFECT (1.0) similarity to the query
    await chunk_repo.create_chunks(
        doc_b.id,
        [
            {
                "chunk_index": 0,
                "content": "Student B confidential chunk",
                "page_number": 1,
                "token_count": 30,
                "embedding": query_vector,  # 1.0 exact match!
            }
        ],
    )

    # Student A executes search
    results_a = await chunk_repo.search_similar_chunks(
        user_id=student_a_id,
        query_embedding=query_vector,
        limit=10,
    )

    # Results MUST contain ONLY Student A's chunk
    assert len(results_a) == 1
    found_chunk, _score = results_a[0]
    assert found_chunk.document_id == doc_a.id
    assert found_chunk.content == "Student A study chunk"

    # Student B executes search
    results_b = await chunk_repo.search_similar_chunks(
        user_id=student_b_id,
        query_embedding=query_vector,
        limit=10,
    )
    assert len(results_b) == 1
    assert results_b[0][0].document_id == doc_b.id
    assert results_b[0][0].content == "Student B confidential chunk"
    assert abs(results_b[0][1] - 1.0) < 1e-4

    # Search with min_similarity threshold filtering
    filtered_results = await chunk_repo.search_similar_chunks(
        user_id=student_a_id,
        query_embedding=query_vector,
        min_similarity=0.99,  # Higher than Student A's similarity
    )
    assert len(filtered_results) == 0


@pytest.mark.asyncio
async def test_sql_chunk_repo_statement_generation() -> None:
    """Verifies that SQL DocumentChunkRepository compiles proper pgvector and PG queries."""
    mock_session = AsyncMock()
    repo = DocumentChunkRepository(session=mock_session)

    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    query_vec = [0.1] * 768

    # Test search query execution
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.execute.return_value = mock_result

    results = await repo.search_similar_chunks(
        user_id=user_id,
        query_embedding=query_vec,
        limit=5,
        min_similarity=0.5,
        document_id=doc_id,
    )
    assert results == []
    mock_session.execute.assert_called_once()

    # Test create_chunks execution
    mock_scalar_result = MagicMock()
    mock_scalar_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_scalar_result

    chunks_data = [
        {"chunk_index": 0, "content": "Sample", "page_number": 1, "token_count": 10, "embedding": query_vec}
    ]
    created = await repo.create_chunks(doc_id, chunks_data)
    assert created == []
    assert mock_session.execute.call_count == 2
    mock_session.flush.assert_called()


# ---------------------------------------------------------------------------
# Part 3: Document Ingestion Pipeline Integration Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_doc_repo() -> FakeDocumentRepository:
    return FakeDocumentRepository()


@pytest.fixture
def fake_chunk_repo(fake_doc_repo: FakeDocumentRepository) -> FakeDocumentChunkRepository:
    return FakeDocumentChunkRepository(document_repo=fake_doc_repo)


@pytest.fixture
def fake_storage() -> FakeStorageClient:
    return FakeStorageClient()


@pytest.fixture
def test_embedding_adapter() -> EmbeddingAdapter:
    return EmbeddingAdapter(test_mode=True)


@pytest.fixture
def document_service(
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    fake_storage: FakeStorageClient,
    test_embedding_adapter: EmbeddingAdapter,
) -> DocumentService:
    course_repo = MagicMock(spec=CourseRepository)
    return DocumentService(
        document_repo=fake_doc_repo,
        course_repo=course_repo,
        storage_client=fake_storage,
        chunk_repo=fake_chunk_repo,
        embedding_adapter=test_embedding_adapter,
        extraction_service=get_extraction_service(),
        chunking_service=get_chunking_service(),
    )


@pytest.mark.asyncio
async def test_process_document_ingestion_pdf_success(
    document_service: DocumentService,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    fake_storage: FakeStorageClient,
) -> None:
    """Verifies successful ingestion of a multi-page PDF study document.

    Checks:
    - Status transitions: pending -> processing -> embedded.
    - Page count extracted.
    - Chunks created with sequential indices and 768-dim embeddings.
    - Constraints satisfied: ck_documents_embedded_complete (total_chunks > 0 and chunks_processed == total_chunks).
    """
    user_id = uuid.uuid4()
    pdf_bytes = _create_pdf_bytes("First chapter of Operating Systems notes.", num_pages=2)
    object_key = "documents/os_notes.pdf"
    fake_storage.upload_file(object_key, pdf_bytes)

    doc = await fake_doc_repo.create(
        user_id=user_id,
        title="OS Notes",
        original_file_name="os_notes.pdf",
        storage_object_key=object_key,
        file_type="application/pdf",
        file_size_bytes=len(pdf_bytes),
        status="pending",
    )

    # Ingest document
    result_doc = await document_service.process_document_ingestion(
        document_id=doc.id,
        user_id=user_id,
    )

    # Verify document record
    assert result_doc.status == "embedded"
    assert result_doc.page_count == 2
    assert result_doc.total_chunks > 0
    assert result_doc.chunks_processed == result_doc.total_chunks
    assert result_doc.processing_error_code is None

    # Verify chunks stored in pgvector repo
    chunks = await fake_chunk_repo.get_chunks_by_document(doc.id)
    assert len(chunks) == result_doc.total_chunks
    for idx, chunk in enumerate(chunks):
        assert chunk.chunk_index == idx
        assert chunk.document_id == doc.id
        assert len(chunk.embedding) == 768
        assert chunk.page_number in (1, 2)


@pytest.mark.asyncio
async def test_process_document_ingestion_docx_success(
    document_service: DocumentService,
    fake_doc_repo: FakeDocumentRepository,
    fake_chunk_repo: FakeDocumentChunkRepository,
    fake_storage: FakeStorageClient,
) -> None:
    """Verifies successful ingestion of a DOCX document."""
    user_id = uuid.uuid4()
    docx_bytes = _create_docx_bytes(["Software Architecture Design", "Invariant 4 and Invariant 6 specifications."])
    object_key = "documents/arch.docx"
    fake_storage.upload_file(object_key, docx_bytes)

    doc = await fake_doc_repo.create(
        user_id=user_id,
        title="Architecture",
        original_file_name="arch.docx",
        storage_object_key=object_key,
        file_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size_bytes=len(docx_bytes),
        status="pending",
    )

    result_doc = await document_service.process_document_ingestion(
        document_id=doc.id,
        user_id=user_id,
    )

    assert result_doc.status == "embedded"
    assert result_doc.total_chunks > 0
    assert result_doc.chunks_processed == result_doc.total_chunks
    assert result_doc.processing_error_code is None


@pytest.mark.asyncio
async def test_process_document_ingestion_empty_document_fails(
    document_service: DocumentService,
    fake_doc_repo: FakeDocumentRepository,
    fake_storage: FakeStorageClient,
) -> None:
    """Verifies that an empty/blank document fails cleanly with EMPTY_DOCUMENT."""
    user_id = uuid.uuid4()
    blank_pdf = _create_blank_pdf_bytes(num_pages=1)
    object_key = "documents/blank.pdf"
    fake_storage.upload_file(object_key, blank_pdf)

    doc = await fake_doc_repo.create(
        user_id=user_id,
        title="Blank PDF",
        original_file_name="blank.pdf",
        storage_object_key=object_key,
        file_type="application/pdf",
        file_size_bytes=len(blank_pdf),
        status="pending",
    )

    result_doc = await document_service.process_document_ingestion(
        document_id=doc.id,
        user_id=user_id,
    )

    assert result_doc.status == "failed"
    assert result_doc.processing_error_code == "EMPTY_DOCUMENT"
    assert result_doc.total_chunks == 0
    assert result_doc.chunks_processed == 0


@pytest.mark.asyncio
async def test_process_document_ingestion_corrupt_file_fails(
    document_service: DocumentService,
    fake_doc_repo: FakeDocumentRepository,
    fake_storage: FakeStorageClient,
) -> None:
    """Verifies corrupt file handling marking status='failed' and processing_error_code='CORRUPT_FILE'."""
    user_id = uuid.uuid4()
    corrupt_bytes = b"NOT_A_VALID_PDF_HEADER_OR_BODY"
    object_key = "documents/corrupt.pdf"
    fake_storage.upload_file(object_key, corrupt_bytes)

    doc = await fake_doc_repo.create(
        user_id=user_id,
        title="Corrupt PDF",
        original_file_name="corrupt.pdf",
        storage_object_key=object_key,
        file_type="application/pdf",
        file_size_bytes=len(corrupt_bytes),
        status="pending",
    )

    result_doc = await document_service.process_document_ingestion(
        document_id=doc.id,
        user_id=user_id,
    )

    assert result_doc.status == "failed"
    assert result_doc.processing_error_code == "CORRUPT_FILE"


@pytest.mark.asyncio
async def test_process_document_ingestion_tenant_isolation_fails(
    document_service: DocumentService,
    fake_doc_repo: FakeDocumentRepository,
) -> None:
    """Verifies that an unauthorized user cannot trigger ingestion for another student's document."""
    student_a_id = uuid.uuid4()
    student_b_id = uuid.uuid4()

    doc = await fake_doc_repo.create(
        user_id=student_a_id,
        title="Student A Notes",
        original_file_name="notes.pdf",
        storage_object_key="documents/notes.pdf",
        file_type="application/pdf",
        file_size_bytes=100,
        status="pending",
    )

    # Student B tries to trigger ingestion
    with pytest.raises(DocumentNotFoundError):
        await document_service.process_document_ingestion(
            document_id=doc.id,
            user_id=student_b_id,
        )


@pytest.mark.asyncio
async def test_process_document_ingestion_storage_file_missing_fails(
    document_service: DocumentService,
    fake_doc_repo: FakeDocumentRepository,
) -> None:
    """Verifies missing storage object marks document failed with STORAGE_FILE_NOT_FOUND."""
    user_id = uuid.uuid4()

    doc = await fake_doc_repo.create(
        user_id=user_id,
        title="Missing File",
        original_file_name="missing.pdf",
        storage_object_key="documents/non_existent.pdf",
        file_type="application/pdf",
        file_size_bytes=100,
        status="pending",
    )

    result_doc = await document_service.process_document_ingestion(
        document_id=doc.id,
        user_id=user_id,
    )

    assert result_doc.status == "failed"
    assert result_doc.processing_error_code == "STORAGE_FILE_NOT_FOUND"


@pytest.mark.asyncio
async def test_process_document_ingestion_raise_on_error_flag(
    document_service: DocumentService,
    fake_doc_repo: FakeDocumentRepository,
    fake_storage: FakeStorageClient,
) -> None:
    """Verifies that when raise_on_error=True, exceptions are re-raised after updating DB."""
    user_id = uuid.uuid4()
    blank_pdf = _create_blank_pdf_bytes(num_pages=1)
    object_key = "documents/blank_raise.pdf"
    fake_storage.upload_file(object_key, blank_pdf)

    doc = await fake_doc_repo.create(
        user_id=user_id,
        title="Blank PDF Raise",
        original_file_name="blank.pdf",
        storage_object_key=object_key,
        file_type="application/pdf",
        file_size_bytes=len(blank_pdf),
        status="pending",
    )

    with pytest.raises(EmptyDocumentError):
        await document_service.process_document_ingestion(
            document_id=doc.id,
            user_id=user_id,
            raise_on_error=True,
        )

    # Check that DB was still updated to 'failed'
    updated = await fake_doc_repo.get_by_id_and_user(doc.id, user_id)
    assert updated is not None
    assert updated.status == "failed"
    assert updated.processing_error_code == "EMPTY_DOCUMENT"


def test_celery_ingest_document_task_execution() -> None:
    """Verifies that Celery background task executes ingestion pipeline."""
    doc_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    mock_doc = MagicMock()
    mock_doc.status = "embedded"

    with patch("app.worker.tasks.document_tasks.async_session_factory") as mock_factory, \
         patch("app.worker.tasks.document_tasks.DocumentService") as mock_service_cls, \
         patch("app.worker.idempotency.get_idempotency_guard") as mock_guard:

        mock_session = AsyncMock()
        mock_factory.return_value.__aenter__.return_value = mock_session

        mock_service = AsyncMock()
        mock_service.process_document_ingestion.return_value = mock_doc
        mock_service_cls.return_value = mock_service

        # Mock idempotency guard to simulate successful lock
        guard_inst = MagicMock()
        guard_inst.acquire_lock.return_value = True
        guard_inst.get_status.return_value = None
        mock_guard.return_value = guard_inst

        result = ingest_document_task(doc_id, user_id)
        assert result["document_id"] == doc_id
        assert result["status"] == "embedded"
