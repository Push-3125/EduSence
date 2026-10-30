"""Unit and integration tests for document text extraction and chunking services (TASK-M3-04).

Covers:
- PDFExtractor: single page, multi-page, page limit cap (MAX_PDF_PAGES = 100), corrupt bytes, empty document.
- DocxExtractor: paragraphs, tables, corrupt bytes, empty document.
- ImageExtractor: PNG, JPEG, corrupt bytes, empty OCR handling.
- ExtractionService: MIME dispatch, filename fallback, unsupported types, DI provider.
- ChunkingService: token estimation, boundary splitting, single page with overlap,
  multi-page page number preservation, DB check constraints compliance.
"""

import io

import docx
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from app.services.chunking_service import (
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_TARGET_TOKENS,
    ChunkingService,
    estimate_tokens,
    get_chunking_service,
)
from app.services.extraction_service import (
    MAX_PDF_PAGES,
    CorruptFileError,
    EmptyDocumentError,
    ExtractedDocument,
    ExtractedPage,
    ExtractionService,
    ImageExtractor,
    PageLimitExceededError,
    UnsupportedFileTypeError,
    get_extraction_service,
)

# ---------------------------------------------------------------------------
# In-Memory Test Artifact Generators
# ---------------------------------------------------------------------------


def _create_pdf_bytes(text: str = "EduSense Sample Content", num_pages: int = 1) -> bytes:
    """Generate in-memory PDF with valid text stream on one or more pages."""
    # Minimal PDF syntax with text stream
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

    reader = PdfReader(io.BytesIO(single_page_pdf))
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_page(reader.pages[0])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _create_blank_pdf_bytes(num_pages: int = 1) -> bytes:
    """Generate in-memory PDF with blank pages (no text content)."""
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _create_docx_bytes(
    paragraphs: list[str] | None = None,
    table_rows: list[list[str]] | None = None,
) -> bytes:
    """Generate in-memory valid DOCX package with paragraphs and/or tables."""
    doc = docx.Document()
    if paragraphs:
        for p in paragraphs:
            doc.add_paragraph(p)
    if table_rows:
        num_cols = max(len(row) for row in table_rows) if table_rows else 1
        table = doc.add_table(rows=0, cols=num_cols)
        for row_data in table_rows:
            row_cells = table.add_row().cells
            for col_idx, cell_value in enumerate(row_data):
                row_cells[col_idx].text = cell_value
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _create_image_bytes(
    format: str = "PNG",
    color: str = "blue",
    size: tuple[int, int] = (120, 80),
) -> bytes:
    """Generate in-memory valid image bytes using PIL."""
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format=format)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PDF Extraction Tests
# ---------------------------------------------------------------------------


def test_pdf_single_page_extraction() -> None:
    """Single page PDF is parsed with page 1 text and correct metadata."""
    pdf_bytes = _create_pdf_bytes("Calculus I Syllabus and Schedule", num_pages=1)
    service = get_extraction_service()

    result = service.extract(pdf_bytes, file_type="application/pdf")

    assert isinstance(result, ExtractedDocument)
    assert result.total_pages == 1
    assert len(result.pages) == 1
    assert result.pages[0].page_number == 1
    assert "Calculus I Syllabus and Schedule" in result.pages[0].text
    assert "Calculus I Syllabus and Schedule" in result.full_text
    assert result.file_type == "application/pdf"


def test_pdf_multi_page_extraction() -> None:
    """Multi-page PDF preserves sequential 1-indexed page numbers."""
    pdf_bytes = _create_pdf_bytes("Distributed Systems Lecture Note", num_pages=3)
    service = get_extraction_service()

    result = service.extract(pdf_bytes, file_type="application/pdf")

    assert result.total_pages == 3
    assert len(result.pages) == 3
    for idx, page in enumerate(result.pages, start=1):
        assert page.page_number == idx
        assert "Distributed Systems Lecture Note" in page.text
    assert "Distributed Systems Lecture Note" in result.full_text


def test_pdf_page_limit_exceeded_error() -> None:
    """PDF exceeding MAX_PDF_PAGES (100 per DECISION-PROP-03) raises PageLimitExceededError."""
    # Create 101 pages PDF
    pdf_bytes = _create_pdf_bytes("Page limit check", num_pages=MAX_PDF_PAGES + 1)
    service = get_extraction_service()

    with pytest.raises(PageLimitExceededError) as exc_info:
        service.extract(pdf_bytes, file_type="application/pdf")

    assert "exceeds maximum page limit of 100 pages" in str(exc_info.value)
    assert f"found {MAX_PDF_PAGES + 1} pages" in str(exc_info.value)


def test_pdf_corrupt_bytes_error() -> None:
    """Corrupted/invalid binary bytes raise CorruptFileError."""
    corrupt_bytes = b"NOT_A_PDF_STREAM_RANDOM_GARBAGE_BYTES_12345"
    service = get_extraction_service()

    with pytest.raises(CorruptFileError) as exc_info:
        service.extract(corrupt_bytes, file_type="application/pdf")

    assert "Corrupt or invalid PDF file" in str(exc_info.value)


def test_pdf_empty_document_error() -> None:
    """Empty PDF bytes (0 bytes) raise EmptyDocumentError."""
    service = get_extraction_service()

    with pytest.raises(EmptyDocumentError) as exc_info:
        service.extract(b"", file_type="application/pdf")

    assert "empty" in str(exc_info.value).lower()


def test_pdf_blank_pages_raise_empty_document_error() -> None:
    """PDF with pages that have no extractable text raises EmptyDocumentError."""
    blank_pdf = _create_blank_pdf_bytes(num_pages=2)
    service = get_extraction_service()

    with pytest.raises(EmptyDocumentError) as exc_info:
        service.extract(blank_pdf, file_type="application/pdf")

    assert "no extractable text" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# DOCX Extraction Tests
# ---------------------------------------------------------------------------


def test_docx_paragraphs_and_tables_extraction() -> None:
    """DOCX extracting both paragraph runs and table cell rows."""
    paragraphs = [
        "Introduction to Algorithms",
        "Chapter 1: Asymptotic Analysis and Big-O notation.",
    ]
    tables = [
        ["Algorithm", "Time Complexity", "Space Complexity"],
        ["Merge Sort", "O(N log N)", "O(N)"],
        ["Quick Sort", "O(N log N)", "O(log N)"],
    ]
    docx_bytes = _create_docx_bytes(paragraphs=paragraphs, table_rows=tables)
    service = get_extraction_service()

    result = service.extract(
        docx_bytes,
        file_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert result.total_pages == 1
    assert len(result.pages) == 1
    assert result.pages[0].page_number == 1
    assert "Introduction to Algorithms" in result.full_text
    assert "Chapter 1: Asymptotic Analysis" in result.full_text
    assert "Merge Sort | O(N log N) | O(N)" in result.full_text
    assert "Quick Sort | O(N log N) | O(log N)" in result.full_text


def test_docx_corrupt_bytes_error() -> None:
    """Corrupt DOCX binary raises CorruptFileError."""
    corrupt_bytes = b"PK\x03\x04INVALID_ZIP_ARCHIVE_DATA_NOT_DOCX"
    service = get_extraction_service()

    with pytest.raises(CorruptFileError) as exc_info:
        service.extract(
            corrupt_bytes,
            file_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    assert "Corrupt or invalid DOCX" in str(exc_info.value)


def test_docx_empty_document_error() -> None:
    """0-byte DOCX payload raises EmptyDocumentError."""
    service = get_extraction_service()

    with pytest.raises(EmptyDocumentError):
        service.extract(
            b"",
            file_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


def test_docx_empty_content_error() -> None:
    """Valid DOCX package without paragraphs or table cells raises EmptyDocumentError."""
    empty_docx = _create_docx_bytes(paragraphs=[], table_rows=[])
    service = get_extraction_service()

    with pytest.raises(EmptyDocumentError) as exc_info:
        service.extract(
            empty_docx,
            file_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    assert "no extractable text" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Image Extraction Tests
# ---------------------------------------------------------------------------


def test_image_valid_png_with_ocr() -> None:
    """Valid PNG with mock OCR handler extracts text and preserves image MIME type."""
    png_bytes = _create_image_bytes(format="PNG", color="green")

    def mock_ocr(img: Image.Image) -> str:
        return "Optical Character Recognition sample result from PNG"

    image_extractor = ImageExtractor(ocr_handler=mock_ocr)
    service = ExtractionService(image_extractor=image_extractor)

    result = service.extract(png_bytes, file_type="image/png")

    assert result.total_pages == 1
    assert result.pages[0].page_number == 1
    assert result.full_text == "Optical Character Recognition sample result from PNG"
    assert result.file_type == "image/png"


def test_image_valid_jpeg_with_ocr() -> None:
    """Valid JPEG with mock OCR handler extracts text properly."""
    jpeg_bytes = _create_image_bytes(format="JPEG", color="red")

    def mock_ocr(img: Image.Image) -> str:
        return "Machine Learning diagram formula: y = Wx + b"

    image_extractor = ImageExtractor(ocr_handler=mock_ocr)
    service = ExtractionService(image_extractor=image_extractor)

    result = service.extract(jpeg_bytes, file_type="image/jpeg")

    assert result.total_pages == 1
    assert "Machine Learning diagram formula" in result.full_text
    assert result.file_type == "image/jpeg"


def test_image_corrupt_bytes_error() -> None:
    """Corrupted image bytes raise CorruptFileError."""
    corrupt_bytes = b"\x89PNG\r\n\x1a\nGARBAGE_PAYLOAD_NOT_IMAGE"
    service = get_extraction_service()

    with pytest.raises(CorruptFileError) as exc_info:
        service.extract(corrupt_bytes, file_type="image/png")

    assert "Corrupt or invalid image file" in str(exc_info.value)


def test_image_empty_bytes_error() -> None:
    """Empty image bytes raise EmptyDocumentError."""
    service = get_extraction_service()

    with pytest.raises(EmptyDocumentError):
        service.extract(b"", file_type="image/png")


def test_image_empty_ocr_result_raises_empty_document_error() -> None:
    """Valid image where OCR produces no text raises EmptyDocumentError."""
    png_bytes = _create_image_bytes(format="PNG")

    def mock_empty_ocr(img: Image.Image) -> str:
        return ""

    image_extractor = ImageExtractor(ocr_handler=mock_empty_ocr)
    service = ExtractionService(image_extractor=image_extractor)

    with pytest.raises(EmptyDocumentError) as exc_info:
        service.extract(png_bytes, file_type="image/png")

    assert "contains no extractable text" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ExtractionService Dispatch & Fallback Tests
# ---------------------------------------------------------------------------


def test_extraction_service_filename_fallback() -> None:
    """Generic or missing MIME type falls back to filename extension."""
    pdf_bytes = _create_pdf_bytes("Inferred from filename", num_pages=1)
    service = get_extraction_service()

    # Pass application/octet-stream with filename 'assignment.pdf'
    result = service.extract(
        pdf_bytes,
        file_type="application/octet-stream",
        filename="lecture_notes.pdf",
    )

    assert result.total_pages == 1
    assert "Inferred from filename" in result.full_text


def test_extraction_service_unsupported_file_type() -> None:
    """Unsupported MIME type raises UnsupportedFileTypeError."""
    service = get_extraction_service()

    with pytest.raises(UnsupportedFileTypeError) as exc_info:
        service.extract(b"arbitrary payload", file_type="application/x-executable")

    assert "Unsupported file type" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Token Estimation Tests
# ---------------------------------------------------------------------------


def test_token_estimation_empty_and_whitespace() -> None:
    """Empty or whitespace-only strings return 0 tokens."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("   \n\n\t  ") == 0


def test_token_estimation_short_text() -> None:
    """Short phrases return positive token counts proportional to content."""
    count = estimate_tokens("Artificial Intelligence and Machine Learning")
    assert 5 <= count <= 12


def test_token_estimation_calibration() -> None:
    """A ~1800 character text yields ~400-500 tokens matching target chunk expectations."""
    sample_text = (
        "Operating systems provide a layer of abstraction between computer hardware and user applications. "
        "Key concepts include processes, threads, virtual memory management, and file systems. "
        "Concurrency control requires understanding race conditions, mutex locks, and semaphores. "
    ) * 8
    char_len = len(sample_text)
    token_est = estimate_tokens(sample_text)

    # Standard ratio is ~4 chars per token -> ~450 tokens for 1800-2000 chars
    assert 1500 <= char_len <= 2500
    assert 350 <= token_est <= 600


# ---------------------------------------------------------------------------
# Chunking Tests
# ---------------------------------------------------------------------------


def test_chunk_text_empty_input() -> None:
    """Empty or blank text produces empty list of chunks."""
    service = get_chunking_service()
    assert service.chunk_text("") == []
    assert service.chunk_text("   \n\n\t  ") == []


def test_chunk_text_single_short_chunk() -> None:
    """Text under target token limit produces exactly one chunk with index 0."""
    service = get_chunking_service()
    text = "Short study note about Newton's third law of motion: every action has an equal reaction."

    chunks = service.chunk_text(text, page_number=2, target_tokens=450)

    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0
    assert chunks[0].page_number == 2
    assert chunks[0].content == text
    assert chunks[0].token_count == estimate_tokens(text)


def test_chunk_text_multi_chunk_with_overlap() -> None:
    """Long text produces multiple overlapping chunks satisfying length expectations."""
    service = get_chunking_service()
    # Build text of ~3000 chars (~700 tokens)
    sentence = "Deep neural networks are trained using backpropagation and stochastic gradient descent. "
    text = (sentence * 35).strip()

    chunks = service.chunk_text(
        text,
        page_number=1,
        target_tokens=200,
        overlap_tokens=40,
    )

    assert len(chunks) >= 3
    for idx, chunk in enumerate(chunks):
        assert chunk.chunk_index == idx
        assert chunk.page_number == 1
        assert len(chunk.content.strip()) > 0
        assert chunk.token_count > 0

    # Verify overlap exists between consecutive chunks:
    # A substring of chunk 0 should appear in chunk 1
    words_chunk_0 = set(chunks[0].content.split())
    words_chunk_1 = set(chunks[1].content.split())
    common_words = words_chunk_0.intersection(words_chunk_1)
    assert len(common_words) > 5, "Adjacent chunks must share overlapping content."


def test_chunk_text_boundary_splitting_paragraphs_and_sentences() -> None:
    """Chunking respects paragraph breaks and sentence terminators, avoiding mid-word splits."""
    service = get_chunking_service()
    para_1 = "Paragraph one introduces database transactions and ACID properties. " * 5
    para_2 = "Paragraph two explains isolation levels like Read Committed and Serializable. " * 5
    para_3 = "Paragraph three explores two-phase locking protocols. " * 5
    full_text = f"{para_1}\n\n{para_2}\n\n{para_3}"

    chunks = service.chunk_text(full_text, target_tokens=150, overlap_tokens=30)

    for chunk in chunks:
        # None of the chunks should start or end with broken words
        assert not chunk.content.startswith(" ")
        assert not chunk.content.endswith(" ")
        # Ensure proper punctuation or end of paragraph
        last_char = chunk.content[-1]
        assert last_char in (".", "!", "?", "\n") or last_char.isalnum()


def test_chunk_document_multi_page_preserves_page_numbers() -> None:
    """ExtractedDocument with multiple pages assigns sequential indices and preserves page numbers."""
    pages = [
        ExtractedPage(page_number=1, text="Page 1 Content on Software Engineering models. " * 10),
        ExtractedPage(page_number=2, text="Page 2 Content on Agile Scrum ceremonies. " * 10),
        ExtractedPage(page_number=3, text="Page 3 Content on Test Driven Development. " * 10),
    ]
    extracted = ExtractedDocument(
        pages=pages,
        total_pages=3,
        full_text="\n\n".join(p.text for p in pages),
        file_type="application/pdf",
    )

    service = get_chunking_service()
    chunks = service.chunk_document(extracted, target_tokens=100, overlap_tokens=20)

    assert len(chunks) >= 3
    # Verify sequential chunk indices: 0, 1, 2, ...
    for idx, chunk in enumerate(chunks):
        assert chunk.chunk_index == idx
        assert chunk.token_count > 0
        assert len(chunk.content.strip()) > 0

    # Verify all 3 pages are represented
    page_numbers = {chunk.page_number for chunk in chunks}
    assert page_numbers == {1, 2, 3}


def test_chunk_document_skips_blank_pages() -> None:
    """Blank pages within an ExtractedDocument are skipped without breaking chunk_index."""
    pages = [
        ExtractedPage(page_number=1, text="Page 1 has valid text."),
        ExtractedPage(page_number=2, text="   \n\n\t  "),  # Blank page
        ExtractedPage(page_number=3, text="Page 3 has valid text."),
    ]
    extracted = ExtractedDocument(
        pages=pages,
        total_pages=3,
        full_text="Page 1 has valid text.\n\nPage 3 has valid text.",
        file_type="application/pdf",
    )

    service = get_chunking_service()
    chunks = service.chunk_document(extracted)

    assert len(chunks) == 2
    assert chunks[0].chunk_index == 0
    assert chunks[0].page_number == 1
    assert chunks[1].chunk_index == 1
    assert chunks[1].page_number == 3


def test_chunk_database_check_constraints_enforced() -> None:
    """Validate that every generated chunk strictly complies with DB check constraints."""
    text = (
        "Linear algebra fundamentals include vector spaces, inner products, linear transformations, "
        "and matrix decompositions such as Singular Value Decomposition (SVD) and Eigenvalue Decomposition. "
    ) * 15

    service = ChunkingService(default_target_tokens=DEFAULT_TARGET_TOKENS, default_overlap_tokens=DEFAULT_OVERLAP_TOKENS)
    chunks = service.chunk_text(text, page_number=1)

    for chunk in chunks:
        # Constraint: ck_document_chunks_index CHECK (chunk_index >= 0)
        assert chunk.chunk_index >= 0
        # Constraint: ck_document_chunks_page CHECK (page_number IS NULL OR page_number > 0)
        assert chunk.page_number is None or chunk.page_number > 0
        # Constraint: ck_document_chunks_tokens CHECK (token_count IS NULL OR token_count >= 0)
        assert chunk.token_count is None or chunk.token_count >= 0
        # Constraint: ck_document_chunks_content CHECK (length(btrim(content)) > 0)
        assert len(chunk.content.strip()) > 0


def test_chunk_service_invalid_parameters_handling() -> None:
    """Invalid parameter bounds are safely validated or clamped."""
    service = get_chunking_service()

    with pytest.raises(ValueError):
        service.chunk_text("Sample", target_tokens=0)

    with pytest.raises(ValueError):
        service.chunk_text("Sample", overlap_tokens=-5)

    # When overlap >= target, it should clamp gracefully without raising or looping infinitely
    chunks = service.chunk_text("Sample text " * 50, target_tokens=100, overlap_tokens=150)
    assert len(chunks) > 0
