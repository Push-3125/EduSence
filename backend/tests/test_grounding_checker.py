"""Comprehensive tests for GroundingChecker & Citation Verification Service (TASK-M4-07).

Verifies:
- Invariant 5: Retrieval is separate from Generation: every LLM answer must pass GroundingChecker
  before being returned. Citations must come ONLY from retrieved chunks; never fabricate sources.
- Invariant 6: Strict tenant isolation. Citations link assistant messages directly to authorized chunks.
- US 2.14: Document-grounded retrieval. Answers only from documents with citations to specific passages.
  If nothing found, explicitly returns fallback "no information found".
- Table answer_citations constraints and repository methods:
  * Check constraints: ck_answer_citations_order (> 0), ck_answer_citations_score (BETWEEN 0 AND 1).
  * Unique constraints: uq_answer_citations_message_chunk, uq_answer_citations_order.
  * CitationRepository: create_citations, get_citations_by_message, delete_citations_by_message.
- GroundingChecker:
  * Fallback when retrieved chunks is empty.
  * Faithful response with [1], [2] citations matching chunks.
  * Hallucinated citations ([99], invalid doc title, fake UUID) discarded and flagged.
  * Overlap scoring (token containment and bigram overlap) and relevance score in [0.0, 1.0].
  * Low grounding score triggers unsupported claims and warning disclaimer.
  * Multi-citation bracket parsing ([1, 2]).
  * Markdown link resilience.
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
from sqlalchemy import CheckConstraint, Table, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.citation_repo import (
    AnswerCitation,
    CitationRepository,
    get_citation_repo,
)
from app.services.grounding_checker import (
    FALLBACK_NO_INFO_TEXT,
    GroundingChecker,
    compute_ngram_overlap,
    get_grounding_checker,
    normalize_text,
    split_into_claims,
    tokenize,
)
from app.services.retrieval_service import RetrievedChunk

# ---------------------------------------------------------------------------
# Test Doubles & Fakes
# ---------------------------------------------------------------------------


class FakeCitationRepository:
    """In-memory test double implementing CitationRepository contract."""

    def __init__(self) -> None:
        # Key: message_id -> list of AnswerCitation
        self.citations: dict[uuid.UUID, list[AnswerCitation]] = {}

    async def create_citations(
        self,
        message_id: uuid.UUID,
        citations_data: list[dict],
    ) -> list[AnswerCitation]:
        if not citations_data:
            return []

        seen_chunks: set[uuid.UUID] = set()
        deduped = []
        for item in citations_data:
            cid = item["chunk_id"]
            if cid not in seen_chunks:
                seen_chunks.add(cid)
                deduped.append(item)

        created: list[AnswerCitation] = []
        for idx, item in enumerate(deduped, start=1):
            order = item.get("citation_order") or idx
            score = item.get("relevance_score")
            if score is not None:
                score = round(max(0.0, min(1.0, float(score))), 4)

            citation = AnswerCitation(
                id=item.get("id") or uuid.uuid4(),
                message_id=message_id,
                chunk_id=item["chunk_id"],
                citation_order=order,
                relevance_score=score,
                quoted_text=item.get("quoted_text"),
            )
            created.append(citation)

        self.citations.setdefault(message_id, []).extend(created)
        return created

    async def get_citations_by_message(
        self,
        message_id: uuid.UUID,
    ) -> list[AnswerCitation]:
        citations = self.citations.get(message_id, [])
        return sorted(citations, key=lambda c: c.citation_order)

    async def delete_citations_by_message(
        self,
        message_id: uuid.UUID,
    ) -> int:
        removed = self.citations.pop(message_id, [])
        return len(removed)


def make_sample_chunk(
    title: str = "Tài liệu Toán học",
    page: int = 1,
    content: str = "Định lý Pythagore phát biểu rằng trong tam giác vuông, bình phương cạnh huyền bằng tổng bình phương hai cạnh góc vuông.",
    similarity: float = 0.85,
    chunk_index: int = 0,
) -> RetrievedChunk:
    """Helper to generate a sample RetrievedChunk."""
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title=title,
        chunk_index=chunk_index,
        page_number=page,
        content=content,
        similarity_score=similarity,
    )


# ---------------------------------------------------------------------------
# Part 1: AnswerCitation Model & Schema Integrity Tests
# ---------------------------------------------------------------------------


def test_answer_citation_table_metadata() -> None:
    """Verify table name, check constraints, unique constraints, and foreign keys."""
    table = AnswerCitation.__table__
    assert isinstance(table, Table)
    assert table.name == "answer_citations"

    # Check constraints
    check_constraints = {c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)}
    assert "ck_answer_citations_order" in check_constraints
    assert "citation_order > 0" in check_constraints["ck_answer_citations_order"]

    assert "ck_answer_citations_score" in check_constraints
    assert "relevance_score" in check_constraints["ck_answer_citations_score"]

    # Unique constraints
    unique_constraints = {
        c.name: [col.name for col in c.columns]
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_answer_citations_message_chunk" in unique_constraints
    assert unique_constraints["uq_answer_citations_message_chunk"] == ["message_id", "chunk_id"]

    assert "uq_answer_citations_order" in unique_constraints
    assert unique_constraints["uq_answer_citations_order"] == ["message_id", "citation_order"]

    # Foreign keys
    fk_targets = {fk.target_fullname for fk in table.foreign_keys}
    assert "ai_messages.id" in fk_targets
    assert "document_chunks.id" in fk_targets


def test_answer_citation_instance_creation() -> None:
    """Verifies AnswerCitation instantiation with valid fields."""
    msg_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    citation = AnswerCitation(
        message_id=msg_id,
        chunk_id=chunk_id,
        citation_order=1,
        relevance_score=0.92,
        quoted_text="Định lý Pythagore",
    )
    assert citation.message_id == msg_id
    assert citation.chunk_id == chunk_id
    assert citation.citation_order == 1
    assert citation.relevance_score == 0.92
    assert citation.quoted_text == "Định lý Pythagore"


# ---------------------------------------------------------------------------
# Part 2: CitationRepository Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_citation_repo_create_empty() -> None:
    """Calling create_citations with empty data returns empty list."""
    mock_session = AsyncMock(spec=AsyncSession)
    repo = CitationRepository(mock_session)
    result = await repo.create_citations(uuid.uuid4(), [])
    assert result == []
    mock_session.flush.assert_not_called()


@pytest.mark.asyncio
async def test_citation_repo_create_citations_sequential_order() -> None:
    """Verifies create_citations assigns sequential 1, 2, 3... orders and flushes."""
    mock_session = AsyncMock(spec=AsyncSession)
    repo = CitationRepository(mock_session)

    message_id = uuid.uuid4()
    chunk1_id = uuid.uuid4()
    chunk2_id = uuid.uuid4()

    data = [
        {"chunk_id": chunk1_id, "relevance_score": 0.85, "quoted_text": "Quote 1"},
        {"chunk_id": chunk2_id, "relevance_score": 0.72, "quoted_text": "Quote 2"},
    ]

    created = await repo.create_citations(message_id, data)
    assert len(created) == 2
    assert created[0].citation_order == 1
    assert created[0].chunk_id == chunk1_id
    assert created[0].relevance_score == 0.85

    assert created[1].citation_order == 2
    assert created[1].chunk_id == chunk2_id
    assert created[1].relevance_score == 0.72

    assert mock_session.add.call_count == 2
    mock_session.flush.assert_called_once()


@pytest.mark.asyncio
async def test_citation_repo_deduplication_by_chunk() -> None:
    """Verifies duplicate chunk_id in input is deduplicated to satisfy unique constraint."""
    mock_session = AsyncMock(spec=AsyncSession)
    repo = CitationRepository(mock_session)

    message_id = uuid.uuid4()
    duplicate_chunk_id = uuid.uuid4()

    data = [
        {"chunk_id": duplicate_chunk_id, "relevance_score": 0.9, "quoted_text": "First"},
        {"chunk_id": duplicate_chunk_id, "relevance_score": 0.6, "quoted_text": "Duplicate"},
    ]

    created = await repo.create_citations(message_id, data)
    assert len(created) == 1
    assert created[0].quoted_text == "First"
    assert created[0].citation_order == 1


@pytest.mark.asyncio
async def test_citation_repo_relevance_score_clamping() -> None:
    """Verifies relevance_score is clamped to [0.0, 1.0] range."""
    mock_session = AsyncMock(spec=AsyncSession)
    repo = CitationRepository(mock_session)

    message_id = uuid.uuid4()
    data = [
        {"chunk_id": uuid.uuid4(), "relevance_score": 1.5},
        {"chunk_id": uuid.uuid4(), "relevance_score": -0.5},
    ]

    created = await repo.create_citations(message_id, data)
    assert created[0].relevance_score == 1.0
    assert created[1].relevance_score == 0.0


@pytest.mark.asyncio
async def test_citation_repo_get_citations_by_message() -> None:
    """Verifies get_citations_by_message executes ordered select query."""
    mock_session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    fake_citation = AnswerCitation(
        message_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        citation_order=1,
        relevance_score=0.9,
    )
    mock_result.scalars.return_value.unique.return_value.all.return_value = [fake_citation]
    mock_session.execute.return_value = mock_result

    repo = CitationRepository(mock_session)
    citations = await repo.get_citations_by_message(fake_citation.message_id)

    assert len(citations) == 1
    assert citations[0].citation_order == 1
    mock_session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_citation_repo_delete_citations() -> None:
    """Verifies delete_citations_by_message executes delete and returns count."""
    mock_session = AsyncMock(spec=AsyncSession)
    mock_delete_result = MagicMock()
    mock_delete_result.rowcount = 3
    mock_session.execute.return_value = mock_delete_result

    repo = CitationRepository(mock_session)
    deleted = await repo.delete_citations_by_message(uuid.uuid4())

    assert deleted == 3
    mock_session.flush.assert_called_once()


def test_get_citation_repo_provider() -> None:
    """Verifies FastAPI dependency provider."""
    mock_session = AsyncMock(spec=AsyncSession)
    repo = get_citation_repo(mock_session)
    assert isinstance(repo, CitationRepository)
    assert repo.session is mock_session


# ---------------------------------------------------------------------------
# Part 3: Text & Overlap Utilities Tests
# ---------------------------------------------------------------------------


def test_normalize_text_and_tokenize() -> None:
    """Verifies text normalization and tokenization for Vietnamese and English."""
    raw = "  Định lý   Pythagore:  a^2 + b^2 = c^2!  "
    normalized = normalize_text(raw)
    tokens = tokenize(raw)

    assert "Định lý Pythagore" in normalized
    assert "định" in tokens
    assert "lý" in tokens
    assert "pythagore" in tokens


def test_compute_ngram_overlap_exact_match() -> None:
    """Verifies exact verbatim match yields 1.0 overlap score."""
    passage = "Đại số tuyến tính nghiên cứu về không gian vector và ánh xạ tuyến tính."
    score, quote = compute_ngram_overlap(passage, passage)

    assert score == 1.0
    assert quote is not None
    assert "Đại số tuyến tính" in quote


def test_compute_ngram_overlap_disjoint() -> None:
    """Verifies disjoint text yields 0.0 overlap score and no quote."""
    claim = "Lịch sử Việt Nam qua các triều đại phong kiến."
    chunk = "Quantum mechanics describes the physical properties of nature at atomic scales."
    score, quote = compute_ngram_overlap(claim, chunk)

    assert score == 0.0
    assert quote is None


def test_compute_ngram_overlap_partial_and_quote_extraction() -> None:
    """Verifies partial overlap calculates score in [0.0, 1.0] and extracts sentence quote."""
    claim = "Ma trận khả nghịch khi và chỉ khi định thức của nó khác 0."
    chunk = (
        "Trong đại số tuyến tính, định thức đóng vai trò cốt lõi. "
        "Một ma trận vuông khả nghịch khi và chỉ khi định thức của nó khác không. "
        "Nếu định thức bằng 0, ma trận được gọi là ma trận suy biến."
    )
    score, quote = compute_ngram_overlap(claim, chunk)

    assert 0.0 < score <= 1.0
    assert quote is not None
    assert "khả nghịch" in quote


def test_compute_ngram_overlap_explicit_quotes() -> None:
    """Verifies explicit quotes in text are preserved verbatim in quoted_text."""
    claim = 'Theo giáo trình, "không gian Hilbert là không gian tích trong đầy đủ" rất quan trọng.'
    chunk = "Trong giải tích hàm, một không gian Hilbert là không gian tích trong đầy đủ về mặt tô-pô."
    score, quote = compute_ngram_overlap(claim, chunk)

    assert score > 0.0
    assert quote == "không gian Hilbert là không gian tích trong đầy đủ"


def test_split_into_claims() -> None:
    """Verifies sentence splitting while preserving citations."""
    text = (
        "Định lý Pythagore áp dụng cho tam giác vuông [1]. "
        "Trong không gian Euclid, khoảng cách được đo bằng chuẩn L2 [2].\n"
        "Định thức ma trận khác 0 chứng tỏ ma trận khả nghịch [Document: Đại số, Page 10]."
    )
    claims = split_into_claims(text)
    assert len(claims) == 3
    assert "[1]" in claims[0]
    assert "[2]" in claims[1]
    assert "[Document: Đại số, Page 10]" in claims[2]


# ---------------------------------------------------------------------------
# Part 4: GroundingChecker Core Verification Tests (US 2.14 / Invariant 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grounding_checker_empty_retrieved_chunks_fallback() -> None:
    """Verifies US 2.14: empty retrieved chunks returns fallback 'no information found'."""
    checker = GroundingChecker()
    eval_res = await checker.verify_and_ground(
        generated_text="Thuyết tương đối rộng mô tả lực hấp dẫn là độ cong của không-thời gian.",
        retrieved_chunks=[],
    )

    assert not eval_res.is_grounded
    assert eval_res.grounding_score == 0.0
    assert eval_res.verified_citations == []
    assert eval_res.final_text == FALLBACK_NO_INFO_TEXT
    assert len(eval_res.unsupported_claims) == 1


@pytest.mark.asyncio
async def test_grounding_checker_empty_generated_text() -> None:
    """Verifies empty or whitespace-only response handles gracefully."""
    checker = GroundingChecker()
    chunk = make_sample_chunk()
    eval_res = await checker.verify_and_ground(
        generated_text="   ",
        retrieved_chunks=[chunk],
    )

    assert not eval_res.is_grounded
    assert eval_res.grounding_score == 0.0
    assert eval_res.verified_citations == []
    assert eval_res.final_text == ""


@pytest.mark.asyncio
async def test_grounding_checker_faithful_response_with_citations() -> None:
    """Verifies faithful response with [1], [2] citations resolves chunks and scores high."""
    fake_repo = FakeCitationRepository()
    checker = GroundingChecker(citation_repo=fake_repo)  # type: ignore[arg-type]

    chunk1 = make_sample_chunk(
        title="Toán Rời Rạc",
        page=5,
        content="Đồ thị liên thông là đồ thị mà giữa hai đỉnh bất kỳ luôn có đường đi.",
        similarity=0.92,
        chunk_index=0,
    )
    chunk2 = make_sample_chunk(
        title="Cấu Trúc Dữ Liệu",
        page=12,
        content="Cây khung nhỏ nhất của đồ thị có thể tìm được bằng thuật toán Kruskal hoặc Prim.",
        similarity=0.88,
        chunk_index=1,
    )

    text = (
        "Đồ thị liên thông là đồ thị có đường đi giữa hai đỉnh bất kỳ [1]. "
        "Thuật toán Kruskal và Prim được dùng để tìm cây khung nhỏ nhất [2]."
    )

    message_id = uuid.uuid4()
    eval_res = await checker.verify_and_ground(
        generated_text=text,
        retrieved_chunks=[chunk1, chunk2],
        message_id=message_id,
        min_grounding_threshold=0.5,
    )

    # 1. Grounding evaluation checks
    assert eval_res.is_grounded
    assert eval_res.grounding_score >= 0.8
    assert len(eval_res.unsupported_claims) == 0
    assert eval_res.final_text == text.strip()

    # 2. Citations verification
    assert len(eval_res.verified_citations) == 2
    c1, c2 = eval_res.verified_citations
    assert c1.citation_order == 1
    assert c2.citation_order == 2
    assert {c1.chunk_id, c2.chunk_id} == {chunk1.chunk_id, chunk2.chunk_id}
    assert c1.relevance_score > 0.0
    assert c2.relevance_score > 0.0
    assert c1.quoted_text is not None

    # 3. Persistence in repository verified
    persisted = await fake_repo.get_citations_by_message(message_id)
    assert len(persisted) == 2
    assert persisted[0].citation_order == 1
    assert persisted[1].citation_order == 2


@pytest.mark.asyncio
async def test_grounding_checker_hallucinated_citations_discarded() -> None:
    """Verifies citations referencing non-existent chunks ([99]) are discarded and flagged."""
    fake_repo = FakeCitationRepository()
    checker = GroundingChecker(citation_repo=fake_repo)  # type: ignore[arg-type]

    chunk1 = make_sample_chunk(
        title="Vật Lý Đại Cương",
        page=1,
        content="Vận tốc ánh sáng trong chân không là hằng số xấp xỉ 300,000 km/s.",
        similarity=0.90,
    )

    # Note: Only 1 chunk exists, but answer cites [99] and [Doc: Unknown Book]
    text = (
        "Vận tốc ánh sáng trong chân không xấp xỉ 300,000 km/s [1]. "
        "Theo nghiên cứu hư cấu [99], hạt tachyons di chuyển nhanh hơn ánh sáng [Doc: NonExistent Book, Page 99]."
    )

    message_id = uuid.uuid4()
    eval_res = await checker.verify_and_ground(
        generated_text=text,
        retrieved_chunks=[chunk1],
        message_id=message_id,
    )

    # Citation [99] and non-existent book must NOT be verified
    verified_ids = [c.chunk_id for c in eval_res.verified_citations]
    assert verified_ids == [chunk1.chunk_id]
    assert len(eval_res.verified_citations) == 1

    # Hallucinated claim must be flagged as unsupported
    assert len(eval_res.unsupported_claims) == 1
    assert "[99]" in eval_res.unsupported_claims[0]

    # Grounding score reflects hallucinated citation penalty
    assert eval_res.grounding_score < 0.8


@pytest.mark.asyncio
async def test_grounding_checker_low_grounding_score_triggers_warning_disclaimer() -> None:
    """Verifies that grounding score below min_grounding_threshold appends warning disclaimer."""
    checker = GroundingChecker()

    chunk1 = make_sample_chunk(
        title="Triết Học Mác - Lênin",
        content="Vật chất là một phạm trù triết học dùng để chỉ thực tại khách quan.",
        similarity=0.75,
    )

    # 3 claims with no support or citations
    text = (
        "Hôm nay thời tiết Hà Nội rất đẹp. "
        "Giá vàng thế giới hôm nay tăng mạnh. "
        "Công nghệ xe điện đang phát triển nhanh chóng."
    )

    eval_res = await checker.verify_and_ground(
        generated_text=text,
        retrieved_chunks=[chunk1],
        min_grounding_threshold=0.5,
    )

    assert not eval_res.is_grounded
    assert eval_res.grounding_score < 0.5
    assert len(eval_res.unsupported_claims) == 3
    assert "⚠️ **Lưu ý**" in eval_res.final_text
    assert "độ xác thực tài liệu thấp" in eval_res.final_text


@pytest.mark.asyncio
async def test_grounding_checker_multi_citation_bracket() -> None:
    """Verifies multi-citation brackets like [1, 2] resolve both chunks."""
    checker = GroundingChecker()

    chunk1 = make_sample_chunk(
        title="Toán 1",
        content="Ma trận chuyển vị đổi hàng thành cột.",
        similarity=0.85,
    )
    chunk2 = make_sample_chunk(
        title="Toán 2",
        content="Ma trận đối xứng có ma trận chuyển vị bằng chính nó.",
        similarity=0.82,
    )

    text = "Ma trận đối xứng thỏa mãn tính chất đổi hàng thành cột bằng chính nó [1, 2]."

    eval_res = await checker.verify_and_ground(
        generated_text=text,
        retrieved_chunks=[chunk1, chunk2],
    )

    assert eval_res.is_grounded
    assert len(eval_res.verified_citations) == 2
    assert {c.chunk_id for c in eval_res.verified_citations} == {chunk1.chunk_id, chunk2.chunk_id}


@pytest.mark.asyncio
async def test_grounding_checker_markdown_link_resilience() -> None:
    """Verifies markdown links [Google](https://google.com) are not treated as hallucinated citations."""
    checker = GroundingChecker()

    chunk1 = make_sample_chunk(
        title="Công Nghệ Web",
        content="Giao thức HTTP là nền tảng truyền tải dữ liệu trên World Wide Web.",
        similarity=0.90,
    )

    text = "Giao thức HTTP truyền tải dữ liệu trên Web [1]. Chi tiết tại [MDN Web Docs](https://developer.mozilla.org)."

    eval_res = await checker.verify_and_ground(
        generated_text=text,
        retrieved_chunks=[chunk1],
    )

    # [1] is genuine, [MDN Web Docs](...) link is not hallucinated citation
    assert len(eval_res.verified_citations) == 1
    assert eval_res.verified_citations[0].chunk_id == chunk1.chunk_id


@pytest.mark.asyncio
async def test_grounding_checker_document_title_citation_matching() -> None:
    """Verifies citation matching by Document title and page [Doc: Hóa Học, Page 3]."""
    checker = GroundingChecker()

    chunk1 = make_sample_chunk(
        title="Hóa Học Đại Cương",
        page=3,
        content="Phản ứng este hóa giữa axit cacboxylic và ancol tạo ra este và nước.",
        similarity=0.89,
    )

    text = "Phản ứng giữa axit và ancol tạo thành este [Doc: Hóa Học Đại Cương, Page 3]."

    eval_res = await checker.verify_and_ground(
        generated_text=text,
        retrieved_chunks=[chunk1],
    )

    assert eval_res.is_grounded
    assert len(eval_res.verified_citations) == 1
    assert eval_res.verified_citations[0].chunk_id == chunk1.chunk_id
    assert eval_res.verified_citations[0].document_title == "Hóa Học Đại Cương"


@pytest.mark.asyncio
async def test_grounding_checker_uuid_citation_matching() -> None:
    """Verifies citation matching by direct chunk UUID."""
    checker = GroundingChecker()

    chunk1 = make_sample_chunk(
        content="Định luật vạn vật hấp dẫn của Newton.",
        similarity=0.85,
    )

    text = f"Mọi vật trong vũ trụ đều hút nhau bằng một lực [{chunk1.chunk_id}]."

    eval_res = await checker.verify_and_ground(
        generated_text=text,
        retrieved_chunks=[chunk1],
    )

    assert eval_res.is_grounded
    assert len(eval_res.verified_citations) == 1
    assert eval_res.verified_citations[0].chunk_id == chunk1.chunk_id


@pytest.mark.asyncio
async def test_grounding_checker_without_repository_does_not_fail() -> None:
    """Verifies GroundingChecker evaluates cleanly when citation_repo is None."""
    checker = GroundingChecker(citation_repo=None)

    chunk1 = make_sample_chunk(
        content="Hệ điều hành quản lý tài nguyên phần cứng và cung cấp dịch vụ cho các chương trình.",
        similarity=0.91,
    )

    text = "Hệ điều hành quản lý tài nguyên máy tính [1]."

    eval_res = await checker.verify_and_ground(
        generated_text=text,
        retrieved_chunks=[chunk1],
        message_id=uuid.uuid4(),  # message_id provided but repo is None
    )

    assert eval_res.is_grounded
    assert len(eval_res.verified_citations) == 1


def test_get_grounding_checker_provider() -> None:
    """Verifies FastAPI dependency provider for GroundingChecker."""
    mock_repo = MagicMock(spec=CitationRepository)
    checker = get_grounding_checker(mock_repo)
    assert isinstance(checker, GroundingChecker)
    assert checker.citation_repo is mock_repo
