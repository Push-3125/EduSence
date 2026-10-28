"""Document text extraction service for PDF, DOCX, and images (TASK-M3-04).

Extracts raw text and page-level metadata from supported university study document
formats in accordance with DECISION-PROP-03 (max 20 MB, max 100 PDF pages).
"""

import io
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import docx
from PIL import Image
from pypdf import PdfReader

logger = logging.getLogger("edusense.extraction")

# Maximum permitted pages for PDF documents per DECISION-PROP-03
MAX_PDF_PAGES: int = 100


# ---------------------------------------------------------------------------
# Domain Data Structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedPage:
    """Extracted text content from a single page of a document.

    Attributes:
        page_number: 1-indexed page number (satisfies ck_document_chunks_page).
        text: Normalized text content extracted from this page.
    """

    page_number: int
    text: str


@dataclass(frozen=True)
class ExtractedDocument:
    """Full extracted document representation with page-level segments.

    Attributes:
        pages: List of ExtractedPage items preserving per-page text and numbers.
        total_pages: Total count of pages detected in the source document.
        full_text: Aggregated normalized text across all pages.
        file_type: MIME type of the processed source document.
    """

    pages: list[ExtractedPage]
    total_pages: int
    full_text: str
    file_type: str


# ---------------------------------------------------------------------------
# Domain Exceptions
# ---------------------------------------------------------------------------


class ExtractionError(Exception):
    """Base domain exception for document text extraction failures."""


class CorruptFileError(ExtractionError):
    """Raised when file bytes are corrupt or unreadable by underlying parsers."""


class UnsupportedFileTypeError(ExtractionError):
    """Raised when MIME type or extension is not supported for text extraction."""


class PageLimitExceededError(ExtractionError):
    """Raised when document page count exceeds allowed maximum (e.g. >100 for PDF)."""


class EmptyDocumentError(ExtractionError):
    """Raised when document has 0 pages or contains no extractable text."""


# ---------------------------------------------------------------------------
# OCR Handler Protocol & Default Implementation
# ---------------------------------------------------------------------------


class OCRHandler(Protocol):
    """Protocol for OCR engines transforming PIL Image into extracted text."""

    def __call__(self, image: Image.Image) -> str:
        """Process PIL Image and return extracted text."""
        ...


def _default_ocr_engine(image: Image.Image) -> str:
    """Default OCR handler attempting pytesseract with silent fallback.

    Args:
        image: PIL Image instance.

    Returns:
        Extracted text if pytesseract is installed and succeeds, else empty string.
    """
    try:
        import pytesseract  # type: ignore[import-untyped]

        return str(pytesseract.image_to_string(image)).strip()
    except (ImportError, RuntimeError, OSError, ValueError) as exc:
        logger.debug("Default OCR execution unavailable or failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Extractor Implementations
# ---------------------------------------------------------------------------


class PDFExtractor:
    """Extractor for Portable Document Format (.pdf) files using pypdf."""

    def __init__(self, max_pages: int = MAX_PDF_PAGES) -> None:
        self.max_pages = max_pages

    def extract(self, file_bytes: bytes) -> ExtractedDocument:
        """Extract text per page from PDF bytes.

        Args:
            file_bytes: Raw binary content of the PDF.

        Returns:
            ExtractedDocument containing pages, total_pages, full_text.

        Raises:
            EmptyDocumentError: If file has 0 pages or no extractable text.
            PageLimitExceededError: If page count exceeds max_pages.
            CorruptFileError: If bytes are corrupt or not a valid PDF.
        """
        if not file_bytes:
            raise EmptyDocumentError("PDF document is empty (0 bytes).")

        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            if reader.is_encrypted:
                # Attempt decryption with empty password for unencrypted permissions
                try:
                    decrypted = reader.decrypt("")
                    if decrypted == 0:
                        raise CorruptFileError("PDF is encrypted and password-protected.")
                except Exception as exc:
                    raise CorruptFileError(f"PDF is encrypted: {exc}") from exc

            page_count = len(reader.pages)
        except (EmptyDocumentError, CorruptFileError, PageLimitExceededError):
            raise
        except Exception as exc:
            raise CorruptFileError(f"Corrupt or invalid PDF file: {exc}") from exc

        if page_count == 0:
            raise EmptyDocumentError("PDF document has 0 pages.")

        if page_count > self.max_pages:
            raise PageLimitExceededError(
                f"PDF exceeds maximum page limit of {self.max_pages} pages (found {page_count} pages)."
            )

        pages: list[ExtractedPage] = []
        has_any_text = False

        for idx, page in enumerate(reader.pages):
            try:
                raw_text = page.extract_text() or ""
            except (ValueError, KeyError, TypeError, IndexError, OSError, RuntimeError) as exc:
                logger.warning("Failed to extract text from PDF page %d: %s", idx + 1, exc)
                raw_text = ""

            cleaned = self._normalize_whitespace(raw_text)
            if cleaned:
                has_any_text = True
            pages.append(ExtractedPage(page_number=idx + 1, text=cleaned))

        if not has_any_text:
            raise EmptyDocumentError("PDF document contains no extractable text.")

        full_text = "\n\n".join(p.text for p in pages if p.text).strip()

        return ExtractedDocument(
            pages=pages,
            total_pages=page_count,
            full_text=full_text,
            file_type="application/pdf",
        )

    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        """Strip control characters, normalize line breaks and collapse spaces."""
        if not text:
            return ""
        # Standardize line endings to LF
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in text.split("\n")]
        result_lines: list[str] = []
        consecutive_empty = 0
        for line in lines:
            if not line:
                consecutive_empty += 1
                if consecutive_empty <= 1:
                    result_lines.append("")
            else:
                consecutive_empty = 0
                cleaned_line = re.sub(r"[ \t]+", " ", line)
                result_lines.append(cleaned_line)
        return "\n".join(result_lines).strip()


class DocxExtractor:
    """Extractor for Microsoft Word (.docx) files using python-docx."""

    def extract(self, file_bytes: bytes) -> ExtractedDocument:
        """Extract text from paragraphs and table cells in DOCX bytes.

        Args:
            file_bytes: Raw binary content of the DOCX file.

        Returns:
            ExtractedDocument containing pages (page 1), full_text.

        Raises:
            EmptyDocumentError: If file has no extractable text or is 0 bytes.
            CorruptFileError: If file is not a valid DOCX package.
        """
        if not file_bytes:
            raise EmptyDocumentError("DOCX document is empty (0 bytes).")

        try:
            doc = docx.Document(io.BytesIO(file_bytes))
        except (EmptyDocumentError, CorruptFileError):
            raise
        except Exception as exc:
            raise CorruptFileError(f"Corrupt or invalid DOCX document: {exc}") from exc

        parts: list[str] = []

        # Extract paragraphs
        for paragraph in doc.paragraphs:
            text = paragraph.text.strip()
            if text:
                cleaned = re.sub(r"[ \t]+", " ", text)
                parts.append(cleaned)

        # Extract table cells
        for table in doc.tables:
            for row in table.rows:
                row_cells: list[str] = []
                for cell in row.cells:
                    ct = cell.text.strip()
                    if ct and (not row_cells or ct != row_cells[-1]):
                        cleaned = re.sub(r"[ \t]+", " ", ct)
                        row_cells.append(cleaned)
                if row_cells:
                    parts.append(" | ".join(row_cells))

        if not parts:
            raise EmptyDocumentError("DOCX document contains no extractable text.")

        full_text = "\n\n".join(parts).strip()
        pages = [ExtractedPage(page_number=1, text=full_text)]

        return ExtractedDocument(
            pages=pages,
            total_pages=1,
            full_text=full_text,
            file_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


class ImageExtractor:
    """Extractor for raster images (PNG, JPEG, WEBP) using PIL and OCR."""

    def __init__(self, ocr_handler: Callable[[Image.Image], str] | None = None) -> None:
        self.ocr_handler = ocr_handler or _default_ocr_engine

    def extract(self, file_bytes: bytes, file_type: str = "image/png") -> ExtractedDocument:
        """Validate image bytes, inspect dimensions/format, and run OCR extraction.

        Args:
            file_bytes: Raw binary content of the image.
            file_type: Canonical MIME type (e.g. 'image/png', 'image/jpeg').

        Returns:
            ExtractedDocument containing OCR text and single page record.

        Raises:
            EmptyDocumentError: If OCR produces no text or file is 0 bytes.
            CorruptFileError: If bytes are invalid or image cannot be decoded.
            ExtractionError: If OCR processing raises an unexpected exception.
        """
        if not file_bytes:
            raise EmptyDocumentError("Image file is empty (0 bytes).")

        try:
            # 1. Verify byte integrity
            verify_img = Image.open(io.BytesIO(file_bytes))
            verify_img.verify()

            # 2. Re-open for dimensions and OCR execution
            img = Image.open(io.BytesIO(file_bytes))
            width, height = img.size
            if width <= 0 or height <= 0:
                raise CorruptFileError("Image has invalid dimensions (width/height <= 0).")
            img_format = img.format or "IMAGE"
        except (EmptyDocumentError, CorruptFileError):
            raise
        except Exception as exc:
            raise CorruptFileError(f"Corrupt or invalid image file: {exc}") from exc

        # 3. Perform OCR extraction
        try:
            extracted_text = self.ocr_handler(img).strip()
        except Exception as exc:
            raise ExtractionError(f"OCR processing failed: {exc}") from exc

        if not extracted_text:
            raise EmptyDocumentError(
                f"Image ({img_format}, {width}x{height}) contains no extractable text."
            )

        return ExtractedDocument(
            pages=[ExtractedPage(page_number=1, text=extracted_text)],
            total_pages=1,
            full_text=extracted_text,
            file_type=file_type,
        )


# ---------------------------------------------------------------------------
# Main Extraction Service & Dependency Injection
# ---------------------------------------------------------------------------


class ExtractionService:
    """Main extraction service dispatching document parsing by MIME type."""

    def __init__(
        self,
        pdf_extractor: PDFExtractor | None = None,
        docx_extractor: DocxExtractor | None = None,
        image_extractor: ImageExtractor | None = None,
    ) -> None:
        self.pdf_extractor = pdf_extractor or PDFExtractor()
        self.docx_extractor = docx_extractor or DocxExtractor()
        self.image_extractor = image_extractor or ImageExtractor()

    def extract(
        self,
        file_bytes: bytes,
        file_type: str,
        filename: str | None = None,
    ) -> ExtractedDocument:
        """Extract text content and page metadata from binary file payload.

        Args:
            file_bytes: Raw binary content of the file.
            file_type: MIME type string (e.g. 'application/pdf').
            filename: Optional original filename for MIME type extension fallback.

        Returns:
            ExtractedDocument instance containing pages and full text.

        Raises:
            EmptyDocumentError: If document is empty (0 bytes) or has no extractable text.
            UnsupportedFileTypeError: If file type is not supported.
            PageLimitExceededError: If page count exceeds allowed limits.
            CorruptFileError: If file bytes cannot be parsed.
            ExtractionError: Base domain error for extraction failures.
        """
        if not file_bytes:
            raise EmptyDocumentError("Document file is empty (0 bytes).")

        mime = file_type.strip().lower() if file_type else ""

        # If MIME type is missing or generic octet-stream, infer from filename extension
        if (not mime or mime == "application/octet-stream") and filename:
            ext = Path(filename).suffix.lower()
            extension_map = {
                ".pdf": "application/pdf",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
            }
            mime = extension_map.get(ext, mime)

        if mime == "application/pdf":
            return self.pdf_extractor.extract(file_bytes)
        elif mime in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/docx",
            "application/msword",
        ):
            return self.docx_extractor.extract(file_bytes)
        elif mime.startswith("image/"):
            return self.image_extractor.extract(file_bytes, file_type=mime)
        else:
            raise UnsupportedFileTypeError(
                f"Unsupported file type for text extraction: '{file_type}'."
            )

    # Alias for API compatibility across services
    extract_text = extract


def get_extraction_service() -> ExtractionService:
    """FastAPI dependency injection provider for ExtractionService."""
    return ExtractionService()


__all__ = [
    "MAX_PDF_PAGES",
    "CorruptFileError",
    "DocxExtractor",
    "EmptyDocumentError",
    "ExtractedDocument",
    "ExtractedPage",
    "ExtractionError",
    "ExtractionService",
    "ImageExtractor",
    "OCRHandler",
    "PDFExtractor",
    "PageLimitExceededError",
    "UnsupportedFileTypeError",
    "get_extraction_service",
]
