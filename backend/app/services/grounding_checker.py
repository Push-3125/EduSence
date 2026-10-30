"""GroundingChecker and Citation Verification Service (Invariant 5, Invariant 6, US 2.13, US 2.14 / TASK-M4-07).

Adheres strictly to EduSense Invariants:
- Invariant 5: Retrieval is separate from Generation: every LLM answer must pass GroundingChecker
  before being returned. Citations must come ONLY from retrieved chunks; never fabricate sources.
- Invariant 6: Strict tenant isolation. Citations link assistant messages directly to authorized
  retrieved document chunks.
- US 2.14: "Answers only from the documents, with citations to specific passages. If nothing is found,
  explicitly say 'no information found'."
- Table answer_citations: Maps message_id to chunk_id with sequential citation_order and relevance_score.
"""

from __future__ import annotations

import itertools
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends

from app.repositories.citation_repo import (
    CitationRepository,
    get_citation_repo,
)
from app.services.retrieval_service import RetrievedChunk

logger = logging.getLogger("edusense.services.grounding_checker")

# Fallback response when no retrieved chunks are available (US 2.14)
FALLBACK_NO_INFO_TEXT = "Không tìm thấy thông tin phù hợp trong các tài liệu đã chọn để trả lời câu hỏi này."


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class VerifiedCitation:
    """Verified citation referencing a genuine retrieved document chunk."""

    citation_order: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    page_number: int | None
    quoted_text: str | None
    relevance_score: float


@dataclass
class GroundingEvaluation:
    """Evaluation result of grounding and citation verification."""

    is_grounded: bool
    grounding_score: float
    verified_citations: list[VerifiedCitation] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)
    final_text: str = ""


# ---------------------------------------------------------------------------
# Text & Overlap Utilities
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """Normalize unicode and remove excessive whitespace for robust comparison."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase alphanumeric words."""
    clean = normalize_text(text).lower()
    return re.findall(r"\w+", clean)


def compute_ngram_overlap(
    claim_text: str,
    chunk_content: str,
) -> tuple[float, str | None]:
    """Compute token containment and bigram overlap between a claim and chunk content.

    Also extracts the most relevant passage from the chunk content as quoted_text.

    Returns:
        tuple of (overlap_score: float in [0.0, 1.0], quoted_text: str | None)
    """
    claim_clean = re.sub(r"\[[^\]]+\]", "", claim_text).strip()
    tokens_claim = tokenize(claim_clean)
    tokens_chunk = tokenize(chunk_content)

    if not tokens_claim or not tokens_chunk:
        return 0.0, None

    set_claim = set(tokens_claim)
    set_chunk = set(tokens_chunk)
    common_unigrams = set_claim.intersection(set_chunk)
    unigram_containment = len(common_unigrams) / len(set_claim)

    if len(tokens_claim) >= 2 and len(tokens_chunk) >= 2:
        bigrams_claim = set(itertools.pairwise(tokens_claim))
        bigrams_chunk = set(itertools.pairwise(tokens_chunk))
        common_bigrams = bigrams_claim.intersection(bigrams_chunk)
        bigram_overlap = len(common_bigrams) / len(bigrams_claim)
        score = 0.5 * unigram_containment + 0.5 * bigram_overlap
    else:
        score = unigram_containment

    score = round(max(0.0, min(1.0, score)), 4)

    # Quoted text extraction:
    # 1. Check for explicit quotation in quotes
    quote_match = re.search(r'["“«]([^"”»]{8,})["”»]', claim_text)
    if quote_match and quote_match.group(1).lower() in chunk_content.lower():
        quoted_text: str | None = quote_match.group(1).strip()
        return score, quoted_text

    # 2. Extract best matching sentence from chunk content
    sentences = re.split(r"(?<=[.!?\n])\s+", chunk_content.strip())
    best_sentence = ""
    best_s_overlap = -1.0

    for s in sentences:
        s_clean = s.strip()
        if not s_clean:
            continue
        s_tokens = set(tokenize(s_clean))
        if not s_tokens:
            continue
        common = set_claim.intersection(s_tokens)
        s_overlap = len(common) / len(set_claim)
        if s_overlap > best_s_overlap:
            best_s_overlap = s_overlap
            best_sentence = s_clean

    if best_s_overlap > 0.15 and best_sentence:
        quoted_text = best_sentence[:300].strip()
    elif score >= 0.2:
        quoted_text = chunk_content[:200].strip()
    else:
        quoted_text = None

    return score, quoted_text


def split_into_claims(text: str) -> list[str]:
    """Split generated text into individual sentences/claims."""
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    claims: list[str] = []
    for line in lines:
        # Split on sentence boundaries followed by space and capital letter or bracket
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-Ỹ0-9\[])", line)
        for s in sentences:
            s_clean = s.strip()
            if s_clean:
                claims.append(s_clean)
    return claims if claims else [text.strip()]


# ---------------------------------------------------------------------------
# GroundingChecker Service
# ---------------------------------------------------------------------------


class GroundingChecker:
    """Service verifying that LLM responses are strictly grounded in retrieved document chunks."""

    def __init__(self, citation_repo: CitationRepository | None = None) -> None:
        self.citation_repo = citation_repo

    async def verify_and_ground(
        self,
        generated_text: str,
        retrieved_chunks: list[RetrievedChunk],
        message_id: uuid.UUID | None = None,
        min_grounding_threshold: float = 0.5,
    ) -> GroundingEvaluation:
        """Verify LLM response against retrieved chunks, score grounding, and persist citations.

        Workflow:
        1. If retrieved_chunks is empty:
           - Sets is_grounded = False, grounding_score = 0.0.
           - Returns final_text = FALLBACK_NO_INFO_TEXT (US 2.14).
        2. Parses citations from generated_text (e.g. [1], [2], [Doc: ..., Page ...]).
        3. Matches parsed citations against retrieved_chunks:
           - Resolves citations to genuine retrieved chunks. Discards and flags hallucinated citations.
           - Calculates n-gram overlap and relevance_score in [0.0, 1.0].
           - Extracts verbatim quoted_text from source chunks.
        4. Calculates grounding_score (grounded claims and valid citations ratio).
        5. If grounding_score < min_grounding_threshold:
           - Sets is_grounded = False and appends grounding warning disclaimer to final_text.
        6. If message_id is provided and verified citations exist:
           - Persists citations to answer_citations via citation_repo.create_citations.
        7. Returns GroundingEvaluation.

        Args:
            generated_text: Raw LLM response string.
            retrieved_chunks: List of RetrievedChunk instances retrieved for this query.
            message_id: Optional AI message UUID for persisting verified citations.
            min_grounding_threshold: Threshold below which is_grounded = False (default: 0.5).

        Returns:
            GroundingEvaluation containing grounding status, score, verified citations, and text.
        """
        # Step 1: Handle empty retrieved chunks (US 2.14)
        if not retrieved_chunks:
            logger.info("GroundingChecker: No retrieved chunks provided; returning fallback response.")
            unsupported = [generated_text.strip()] if generated_text and generated_text.strip() else []
            return GroundingEvaluation(
                is_grounded=False,
                grounding_score=0.0,
                verified_citations=[],
                unsupported_claims=unsupported,
                final_text=FALLBACK_NO_INFO_TEXT,
            )

        # Handle empty generated text
        if not generated_text or not generated_text.strip():
            return GroundingEvaluation(
                is_grounded=False,
                grounding_score=0.0,
                verified_citations=[],
                unsupported_claims=[],
                final_text="",
            )

        clean_text = generated_text.strip()

        # Step 2 & 3: Chunk lookup indices
        chunk_by_index: dict[int, RetrievedChunk] = {
            i + 1: chunk for i, chunk in enumerate(retrieved_chunks)
        }
        chunk_by_id: dict[uuid.UUID, RetrievedChunk] = {
            chunk.chunk_id: chunk for chunk in retrieved_chunks
        }

        claims = split_into_claims(clean_text)
        total_claims = len(claims)

        # Track verified chunks: chunk_id -> (chunk, quoted_text, relevance_score)
        verified_chunks_map: dict[uuid.UUID, tuple[RetrievedChunk, str | None, float]] = {}
        unsupported_claims: list[str] = []

        total_citation_markers = 0
        valid_citation_markers = 0
        hallucinated_citation_markers = 0

        # Step 3: Match claims and citations against retrieved_chunks
        for claim in claims:
            # Check for bracket citation tags e.g. [1], [2], [1, 2], [Doc: ..., Page ...]
            bracket_contents = re.findall(r"\[([^\]]+)\]", claim)
            claim_has_citations = False
            claim_grounded_by_citation = False
            claim_has_hallucination = False

            for content in bracket_contents:
                content_str = content.strip()

                # Check if it is a markdown link target like [text](url) - ignore if followed by '('
                if re.search(r"\[" + re.escape(content_str) + r"\]\(", claim):
                    continue

                # Pattern A: Direct UUID
                uuid_match = re.fullmatch(
                    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                    content_str,
                )
                if uuid_match:
                    claim_has_citations = True
                    total_citation_markers += 1
                    target_uuid = uuid.UUID(content_str)
                    if target_uuid in chunk_by_id:
                        valid_citation_markers += 1
                        matched_chunk = chunk_by_id[target_uuid]
                        overlap, quote = compute_ngram_overlap(claim, matched_chunk.content)
                        sim = matched_chunk.similarity_score
                        rel_score = round(min(1.0, max(0.0, 0.4 * sim + 0.6 * overlap if sim > 0 else overlap)), 4)
                        if overlap >= 0.05 or rel_score >= 0.1:
                            claim_grounded_by_citation = True
                            if (
                                matched_chunk.chunk_id not in verified_chunks_map
                                or rel_score > verified_chunks_map[matched_chunk.chunk_id][2]
                            ):
                                verified_chunks_map[matched_chunk.chunk_id] = (matched_chunk, quote, rel_score)
                    else:
                        hallucinated_citation_markers += 1
                        claim_has_hallucination = True
                    continue

                # Pattern B: Numeric index e.g. [1], [2], [1, 2], [Chunk 1], [#1]
                num_match = re.match(
                    r"^(?:(?:chunk|doc|ref|source|tài liệu)\s*)?#?(\d+(?:\s*[,;\-]\s*\d+)*)$",
                    content_str,
                    re.IGNORECASE,
                )
                if num_match:
                    claim_has_citations = True
                    # Parse indices
                    parts = re.split(r"[,;\-]", num_match.group(1))
                    indices: list[int] = []
                    for p in parts:
                        p_str = p.strip()
                        if p_str.isdigit():
                            indices.append(int(p_str))

                    for idx in indices:
                        total_citation_markers += 1
                        if idx in chunk_by_index:
                            valid_citation_markers += 1
                            matched_chunk = chunk_by_index[idx]
                            overlap, quote = compute_ngram_overlap(claim, matched_chunk.content)
                            sim = matched_chunk.similarity_score
                            rel_score = round(
                                min(1.0, max(0.0, 0.4 * sim + 0.6 * overlap if sim > 0 else overlap)),
                                4,
                            )
                            # Even with low lexical overlap, explicit citation to retrieved chunk with vector sim counts
                            effective_score = max(rel_score, round(min(1.0, sim * 0.75), 4))
                            claim_grounded_by_citation = True
                            if (
                                matched_chunk.chunk_id not in verified_chunks_map
                                or effective_score > verified_chunks_map[matched_chunk.chunk_id][2]
                            ):
                                verified_chunks_map[matched_chunk.chunk_id] = (
                                    matched_chunk,
                                    quote,
                                    effective_score,
                                )
                        else:
                            hallucinated_citation_markers += 1
                            claim_has_hallucination = True
                    continue

                # Pattern C: Doc title / Page e.g. [Doc: Math 101, Page 5]
                doc_match = re.match(
                    r"^(?:(?:doc|document|tài liệu):\s*)?([^,]+?)(?:,\s*(?:page|trang)\s*(\d+))?$",
                    content_str,
                    re.IGNORECASE,
                )
                if doc_match:
                    title_query = doc_match.group(1).strip().lower()
                    page_query = int(doc_match.group(2)) if doc_match.group(2) else None

                    # Find candidate chunks
                    candidates = [
                        c
                        for c in retrieved_chunks
                        if title_query in c.document_title.lower() or c.document_title.lower() in title_query
                    ]
                    if page_query is not None:
                        page_candidates = [c for c in candidates if c.page_number == page_query]
                        if page_candidates:
                            candidates = page_candidates

                    if candidates:
                        claim_has_citations = True
                        total_citation_markers += 1
                        valid_citation_markers += 1
                        matched_chunk = candidates[0]
                        overlap, quote = compute_ngram_overlap(claim, matched_chunk.content)
                        sim = matched_chunk.similarity_score
                        rel_score = round(
                            min(1.0, max(0.0, 0.4 * sim + 0.6 * overlap if sim > 0 else overlap)),
                            4,
                        )
                        claim_grounded_by_citation = True
                        if (
                            matched_chunk.chunk_id not in verified_chunks_map
                            or rel_score > verified_chunks_map[matched_chunk.chunk_id][2]
                        ):
                            verified_chunks_map[matched_chunk.chunk_id] = (
                                matched_chunk,
                                quote,
                                rel_score,
                            )
                    else:
                        # Does not match any chunk title
                        claim_has_citations = True
                        total_citation_markers += 1
                        hallucinated_citation_markers += 1
                        claim_has_hallucination = True

            # Evaluate claim groundedness
            if claim_has_citations:
                if claim_has_hallucination or not claim_grounded_by_citation:
                    unsupported_claims.append(claim)
            else:
                # No bracket citation: test semantic/token overlap against all retrieved chunks
                best_chunk: RetrievedChunk | None = None
                best_overlap = -1.0
                best_quote: str | None = None

                for chunk in retrieved_chunks:
                    overlap, quote = compute_ngram_overlap(claim, chunk.content)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_chunk = chunk
                        best_quote = quote

                if best_chunk is not None and best_overlap >= 0.25:
                    sim = best_chunk.similarity_score
                    rel_score = round(
                        min(1.0, max(0.0, 0.4 * sim + 0.6 * best_overlap if sim > 0 else best_overlap)),
                        4,
                    )
                    if (
                        best_chunk.chunk_id not in verified_chunks_map
                        or rel_score > verified_chunks_map[best_chunk.chunk_id][2]
                    ):
                        verified_chunks_map[best_chunk.chunk_id] = (
                            best_chunk,
                            best_quote,
                            rel_score,
                        )
                else:
                    unsupported_claims.append(claim)

        # Step 4: Calculate grounding_score
        grounded_claims_count = max(0, total_claims - len(unsupported_claims))
        claim_ratio = grounded_claims_count / total_claims if total_claims > 0 else 0.0

        if total_citation_markers > 0:
            citation_ratio = valid_citation_markers / total_citation_markers
            # Hallucinated citations heavily penalize score
            grounding_score = round(
                max(0.0, min(1.0, 0.5 * claim_ratio + 0.5 * citation_ratio)), 4
            )
        else:
            grounding_score = round(max(0.0, min(1.0, claim_ratio)), 4)

        # Build sorted list of VerifiedCitation instances with sequential citation_order (1, 2, 3...)
        verified_citations: list[VerifiedCitation] = []
        sorted_chunks = sorted(
            verified_chunks_map.values(),
            key=lambda x: (x[2], x[0].chunk_index),
            reverse=True,
        )

        for order, (chunk, quote, rel_score) in enumerate(sorted_chunks, start=1):
            verified_citations.append(
                VerifiedCitation(
                    citation_order=order,
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    document_title=chunk.document_title,
                    page_number=chunk.page_number,
                    quoted_text=quote,
                    relevance_score=rel_score,
                )
            )

        # Step 5: Check grounding threshold and construct final text
        is_grounded = grounding_score >= min_grounding_threshold
        final_text = clean_text

        if not is_grounded:
            disclaimer = (
                f"\n\n> ⚠️ **Lưu ý**: Câu trả lời này có độ xác thực tài liệu thấp "
                f"({int(grounding_score * 100)}%). Một số thông tin có thể không được bảo đảm hoàn toàn từ tài liệu tham khảo."
            )
            if "⚠️ **Lưu ý**" not in final_text and "⚠️ **Cảnh báo**" not in final_text:
                final_text = f"{final_text.rstrip()}{disclaimer}"

        # Step 6: Persist citations if message_id is provided and repository available
        if message_id is not None and self.citation_repo is not None and verified_citations:
            citations_data = [
                {
                    "chunk_id": vc.chunk_id,
                    "citation_order": vc.citation_order,
                    "relevance_score": vc.relevance_score,
                    "quoted_text": vc.quoted_text,
                }
                for vc in verified_citations
            ]
            try:
                await self.citation_repo.create_citations(
                    message_id=message_id,
                    citations_data=citations_data,
                )
                logger.info(
                    "GroundingChecker: Successfully persisted %d citations for message %s.",
                    len(verified_citations),
                    message_id,
                )
            except Exception as exc:
                logger.error(
                    "GroundingChecker: Failed to persist citations for message %s: %s",
                    message_id,
                    exc,
                )
                raise

        # Step 7: Return GroundingEvaluation
        return GroundingEvaluation(
            is_grounded=is_grounded,
            grounding_score=grounding_score,
            verified_citations=verified_citations,
            unsupported_claims=unsupported_claims,
            final_text=final_text,
        )


# ---------------------------------------------------------------------------
# Dependency Provider
# ---------------------------------------------------------------------------


def get_grounding_checker(
    citation_repo: Annotated[CitationRepository, Depends(get_citation_repo)],
) -> GroundingChecker:
    """FastAPI dependency provider for GroundingChecker."""
    return GroundingChecker(citation_repo=citation_repo)


__all__ = [
    "FALLBACK_NO_INFO_TEXT",
    "GroundingChecker",
    "GroundingEvaluation",
    "VerifiedCitation",
    "compute_ngram_overlap",
    "get_grounding_checker",
    "split_into_claims",
]
