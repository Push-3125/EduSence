"""Document chunking service with semantic boundary preservation and overlap (TASK-M3-04).

Implements the document chunking strategy per DECISION-PROP-02:
- Target chunk size: 400-500 tokens (~1500-2000 chars)
- Overlap size: 60-75 tokens (~200-300 chars)
- Boundary hierarchy: Paragraphs (\\n\\n) -> Lines (\\n) -> Sentences (. ! ?) -> Words (space)
- Enforces database constraints: ck_document_chunks_index, ck_document_chunks_page,
  ck_document_chunks_tokens, ck_document_chunks_content.
"""

import logging
import re
from dataclasses import dataclass

from app.services.extraction_service import ExtractedDocument

logger = logging.getLogger("edusense.chunking")

# Default chunking parameters per DECISION-PROP-02
DEFAULT_TARGET_TOKENS: int = 450
DEFAULT_OVERLAP_TOKENS: int = 70


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkData:
    """Decoupled representation of a single text chunk extracted from a document.

    Attributes:
        chunk_index: Sequential 0-indexed position within the document (ck_document_chunks_index >= 0).
        content: Non-empty trimmed text chunk (ck_document_chunks_content).
        page_number: 1-indexed source document page or None (ck_document_chunks_page).
        token_count: Estimated token count of the chunk (ck_document_chunks_tokens >= 0).
    """

    chunk_index: int
    content: str
    page_number: int | None
    token_count: int


# ---------------------------------------------------------------------------
# Token Estimator Heuristic
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Estimate token count for a text string using calibrated heuristic (~4 chars/token).

    Combines character-length and whitespace-delimited word counts to provide a balanced
    estimate across English and multilingual study documents.

    Args:
        text: String content to estimate.

    Returns:
        Estimated integer token count (0 for empty/whitespace string, >= 1 otherwise).
    """
    cleaned = text.strip()
    if not cleaned:
        return 0

    char_count = len(cleaned)
    words = cleaned.split()
    word_count = len(words)

    # Heuristic: 4 characters per token average blended with 1.3 tokens per word
    char_est = char_count / 4.0
    word_est = word_count * 1.3
    tokens = round((char_est + word_est) / 2.0)

    return max(1, tokens)


# ---------------------------------------------------------------------------
# Chunking Service Implementation
# ---------------------------------------------------------------------------


class ChunkingService:
    """Service splitting extracted documents and raw text into semantically cohesive chunks."""

    def __init__(
        self,
        default_target_tokens: int = DEFAULT_TARGET_TOKENS,
        default_overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    ) -> None:
        self.default_target_tokens = default_target_tokens
        self.default_overlap_tokens = default_overlap_tokens

    def estimate_tokens(self, text: str) -> int:
        """Instance method proxy to estimate_tokens."""
        return estimate_tokens(text)

    def chunk_text(
        self,
        text: str,
        page_number: int | None = None,
        target_tokens: int = DEFAULT_TARGET_TOKENS,
        overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    ) -> list[ChunkData]:
        """Split arbitrary text into overlapping chunks respecting semantic boundaries.

        Args:
            text: Raw input text to split.
            page_number: Optional 1-indexed page number to associate with produced chunks.
            target_tokens: Target token count per chunk (default 450).
            overlap_tokens: Desired token overlap between consecutive chunks (default 70).

        Returns:
            List of ChunkData items with 0-based sequential chunk_index.

        Raises:
            ValueError: If target_tokens <= 0 or overlap_tokens < 0.
        """
        cleaned = text.strip()
        if not cleaned:
            return []

        if target_tokens <= 0:
            raise ValueError(f"target_tokens must be > 0 (got {target_tokens}).")
        if overlap_tokens < 0:
            raise ValueError(f"overlap_tokens cannot be negative (got {overlap_tokens}).")
        if overlap_tokens >= target_tokens:
            overlap_tokens = max(0, target_tokens // 5)

        total_est = estimate_tokens(cleaned)
        if total_est <= target_tokens:
            return [
                ChunkData(
                    chunk_index=0,
                    content=cleaned,
                    page_number=page_number,
                    token_count=total_est,
                )
            ]

        chunks: list[ChunkData] = []
        pos = 0
        chunk_idx = 0

        while pos < len(cleaned):
            end = self._find_split_point(cleaned, pos, target_tokens)
            chunk_slice = cleaned[pos:end].strip()

            if chunk_slice:
                chunks.append(
                    ChunkData(
                        chunk_index=chunk_idx,
                        content=chunk_slice,
                        page_number=page_number,
                        token_count=estimate_tokens(chunk_slice),
                    )
                )
                chunk_idx += 1

            if end >= len(cleaned):
                break

            next_pos = self._find_overlap_start(cleaned, pos, end, overlap_tokens)
            if next_pos <= pos or next_pos >= end:
                next_pos = end

            pos = next_pos

        return chunks

    def chunk_document(
        self,
        extracted: ExtractedDocument,
        target_tokens: int = DEFAULT_TARGET_TOKENS,
        overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    ) -> list[ChunkData]:
        """Chunk all pages of an ExtractedDocument while preserving source page numbers.

        Iterates through extracted pages, chunking non-empty pages and maintaining
        strictly sequential chunk indices (0, 1, 2, ...) across the entire document.

        Args:
            extracted: ExtractedDocument containing pages and metadata.
            target_tokens: Target token count per chunk (default 450).
            overlap_tokens: Desired token overlap between consecutive chunks (default 70).

        Returns:
            List of ChunkData items with globally sequential chunk_index.
        """
        all_chunks: list[ChunkData] = []
        current_index = 0

        for page in extracted.pages:
            page_text = page.text.strip()
            if not page_text:
                continue

            page_chunks = self.chunk_text(
                text=page_text,
                page_number=page.page_number,
                target_tokens=target_tokens,
                overlap_tokens=overlap_tokens,
            )

            for chunk in page_chunks:
                all_chunks.append(
                    ChunkData(
                        chunk_index=current_index,
                        content=chunk.content,
                        page_number=chunk.page_number,
                        token_count=chunk.token_count,
                    )
                )
                current_index += 1

        return all_chunks

    @staticmethod
    def _find_split_point(text: str, start: int, target_tokens: int) -> int:
        """Find the optimal semantic boundary in text near target token boundary."""
        target_chars = target_tokens * 4
        target_pos = start + target_chars
        if target_pos >= len(text):
            return len(text)

        min_pos = max(start + 1, start + int(target_chars * 0.70))
        max_pos = min(len(text), start + int(target_chars * 1.15))
        search_window = text[min_pos:max_pos]

        # 1. Paragraph boundary \n\n
        idx = search_window.rfind("\n\n")
        if idx != -1:
            return min_pos + idx + 2

        # 2. Line boundary \n
        idx = search_window.rfind("\n")
        if idx != -1:
            return min_pos + idx + 1

        # 3. Sentence boundary (. ! ?)
        matches = list(re.finditer(r"[.!?]\s+", search_window))
        if matches:
            return min_pos + matches[-1].end()

        # 4. Word boundary ' '
        idx = search_window.rfind(" ")
        if idx != -1:
            return min_pos + idx + 1

        # 5. Search forward for next space if unbroken in primary window
        fwd_space = text.find(" ", max_pos)
        if fwd_space != -1 and (fwd_space - start) < (target_chars * 1.5):
            return fwd_space + 1

        # 6. Hard fallback at target_pos
        return min(len(text), target_pos)

    @staticmethod
    def _find_overlap_start(text: str, start: int, end_pos: int, overlap_tokens: int) -> int:
        """Find a clean semantic starting boundary for the overlapping next chunk."""
        if overlap_tokens <= 0:
            return end_pos

        overlap_chars = overlap_tokens * 4
        nominal_start = end_pos - overlap_chars
        if nominal_start <= start:
            return end_pos

        w_min = max(start + 1, nominal_start - 80)
        w_max = min(end_pos - 1, nominal_start + 80)
        if w_min >= w_max:
            return nominal_start

        window = text[w_min:w_max]

        # 1. Paragraph boundary
        idx = window.find("\n\n")
        if idx != -1:
            return w_min + idx + 2

        # 2. Line boundary
        idx = window.find("\n")
        if idx != -1:
            return w_min + idx + 1

        # 3. Sentence boundary
        matches = list(re.finditer(r"[.!?]\s+", window))
        if matches:
            return w_min + matches[0].end()

        # 4. Word boundary
        idx = window.find(" ")
        if idx != -1:
            return w_min + idx + 1

        return nominal_start


def get_chunking_service() -> ChunkingService:
    """FastAPI dependency injection provider for ChunkingService."""
    return ChunkingService()


__all__ = [
    "DEFAULT_OVERLAP_TOKENS",
    "DEFAULT_TARGET_TOKENS",
    "ChunkData",
    "ChunkingService",
    "estimate_tokens",
    "get_chunking_service",
]
