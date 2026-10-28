"""Document service handling file validation, storage uploads, and lifecycle (US 2.10)."""

import inspect
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, UploadFile

from app.clients.storage_client import (
    MinIOStorageClient,
    StorageError,
    get_storage_client,
)
from app.repositories.course_repo import CourseRepository, get_course_repo
from app.repositories.document_repo import (
    Document,
    DocumentRepository,
    get_document_repo,
)
from app.services.course_service import CourseNotFoundError

logger = logging.getLogger("edusense.documents")

# Maximum upload file size: 20 MB per DECISION-PROP-03
MAX_FILE_SIZE_BYTES: int = 20 * 1024 * 1024

# Permitted MIME types and file extensions
ALLOWED_EXTENSIONS: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

ALLOWED_MIME_TYPES: set[str] = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/png",
    "image/jpeg",
    "image/webp",
}


# ---------------------------------------------------------------------------
# Domain Exceptions
# ---------------------------------------------------------------------------


class DocumentServiceError(Exception):
    """Base exception for document service domain errors."""


class DocumentNotFoundError(DocumentServiceError):
    """Raised when a document is not found or does not belong to the user."""


class DocumentValidationError(DocumentServiceError):
    """Raised when document input fails domain validation rules."""


class FileTooLargeError(DocumentValidationError):
    """Raised when the uploaded file exceeds the 20 MB size limit."""


class UnsupportedFileTypeError(DocumentValidationError):
    """Raised when the uploaded file extension or MIME type is not allowed."""


# ---------------------------------------------------------------------------
# Domain DTO
# ---------------------------------------------------------------------------


@dataclass
class DocumentDTO:
    """Decoupled Data Transfer Object representing a study document."""

    id: uuid.UUID
    user_id: uuid.UUID
    course_id: uuid.UUID | None
    title: str
    original_file_name: str
    storage_object_key: str
    file_type: str
    file_size_bytes: int
    page_count: int | None
    status: str
    total_chunks: int
    chunks_processed: int
    processing_error_code: str | None
    created_at: datetime
    updated_at: datetime
    download_url: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def validate_file_type(filename: str, raw_content_type: str | None) -> str:
    """Validate file extension and MIME type against permitted study document formats.

    Args:
        filename: Original file name.
        raw_content_type: Content-Type string provided in upload headers.

    Returns:
        Verified canonical MIME type string.

    Raises:
        UnsupportedFileTypeError: If file extension or MIME type is not permitted.
    """
    path = Path(filename)
    ext = path.suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"Unsupported file extension '{ext}'. Allowed formats: PDF (.pdf), Word (.docx), Images (.png, .jpg, .jpeg, .webp)."
        )

    expected_mime = ALLOWED_EXTENSIONS[ext]
    cleaned_mime = raw_content_type.strip().lower() if raw_content_type else ""

    if cleaned_mime and cleaned_mime != "application/octet-stream":
        if cleaned_mime not in ALLOWED_MIME_TYPES:
            raise UnsupportedFileTypeError(
                f"Unsupported MIME type '{raw_content_type}'. Allowed types: PDF, DOCX, PNG, JPEG, WEBP."
            )
        # Verify extension family compatibility
        if ext == ".pdf" and cleaned_mime != "application/pdf":
            raise UnsupportedFileTypeError(f"MIME type '{cleaned_mime}' does not match .pdf file extension.")
        if ext == ".docx" and cleaned_mime not in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/zip",
            "application/x-zip-compressed",
        ):
            raise UnsupportedFileTypeError(f"MIME type '{cleaned_mime}' does not match .docx file extension.")
        if ext in (".png", ".jpg", ".jpeg", ".webp") and not cleaned_mime.startswith("image/"):
            raise UnsupportedFileTypeError(f"MIME type '{cleaned_mime}' does not match image file extension.")
        return cleaned_mime

    return expected_mime


def _to_dto(document: Document, download_url: str | None = None) -> DocumentDTO:
    """Map SQLAlchemy Document model to decoupled DocumentDTO."""
    return DocumentDTO(
        id=document.id,
        user_id=document.user_id,
        course_id=document.course_id,
        title=document.title,
        original_file_name=document.original_file_name,
        storage_object_key=document.storage_object_key,
        file_type=document.file_type,
        file_size_bytes=document.file_size_bytes,
        page_count=document.page_count,
        status=document.status,
        total_chunks=document.total_chunks,
        chunks_processed=document.chunks_processed,
        processing_error_code=document.processing_error_code,
        created_at=document.created_at,
        updated_at=document.updated_at,
        download_url=download_url,
    )


# ---------------------------------------------------------------------------
# Document Service Implementation
# ---------------------------------------------------------------------------


class DocumentService:
    """Service encapsulating study document lifecycle, validation, and storage operations."""

    def __init__(
        self,
        document_repo: DocumentRepository,
        course_repo: CourseRepository,
        storage_client: MinIOStorageClient,
    ) -> None:
        self.document_repo = document_repo
        self.course_repo = course_repo
        self.storage_client = storage_client

    async def upload_document(
        self,
        user_id: uuid.UUID,
        file: UploadFile | tuple[str, bytes, str],
        title: str | None = None,
        course_id: uuid.UUID | None = None,
    ) -> DocumentDTO:
        """Validate, store, and persist a new study document upload.

        Args:
            user_id: Authenticated student user UUID.
            file: UploadFile from multipart request or tuple of (filename, bytes, content_type).
            title: Optional custom document title.
            course_id: Optional UUID of the associated course.

        Returns:
            DocumentDTO with metadata and initial status 'pending'.

        Raises:
            UnsupportedFileTypeError: If file extension or MIME type is not allowed.
            FileTooLargeError: If file exceeds 20 MB size limit.
            DocumentValidationError: If file is empty or parameters are invalid.
            CourseNotFoundError: If course_id does not exist or belong to user_id.
            DocumentServiceError: If storage upload fails.
        """
        # 1. Extract file payload
        orig_filename: str
        file_bytes: bytes
        raw_content_type: str | None = None

        if isinstance(file, UploadFile):
            orig_filename = file.filename or "document"
            raw_content_type = file.content_type
            file_bytes = await file.read()
        elif isinstance(file, tuple):
            orig_filename, file_bytes, raw_content_type = file
        elif hasattr(file, "read"):
            orig_filename = getattr(file, "filename", None) or "document"
            raw_content_type = getattr(file, "content_type", None)
            res = file.read()
            if inspect.isawaitable(res):
                file_bytes = await res
            else:
                file_bytes = res
        else:
            raise DocumentValidationError("Invalid file upload payload format.")

        # 2. Validate file size (max 20 MB, > 0 bytes)
        file_size = len(file_bytes)
        if file_size > MAX_FILE_SIZE_BYTES:
            raise FileTooLargeError(
                f"File size ({file_size / (1024 * 1024):.2f} MB) exceeds maximum allowed limit of 20 MB."
            )
        if file_size == 0:
            raise DocumentValidationError("Uploaded file cannot be empty (0 bytes).")

        # 3. Validate file type (PDF, Word, PNG, JPEG, WEBP)
        canonical_mime = validate_file_type(orig_filename, raw_content_type)

        # 4. Verify course ownership if course_id provided
        if course_id is not None:
            course = await self.course_repo.get_by_id_and_user(course_id=course_id, user_id=user_id)
            if not course:
                raise CourseNotFoundError(f"Course '{course_id}' was not found.")

        # 5. Determine title
        clean_title: str
        if title and title.strip():
            clean_title = title.strip()
            if len(clean_title) > 255:
                raise DocumentValidationError("Document title cannot exceed 255 characters.")
        else:
            stem = Path(orig_filename).stem.strip()
            clean_title = stem if stem else orig_filename
            if len(clean_title) > 255:
                clean_title = clean_title[:255]

        # 6. Generate storage key
        doc_id = uuid.uuid4()
        clean_orig_name = Path(orig_filename).name.strip()
        sanitized_filename = re.sub(r"[^a-zA-Z0-9._-]", "_", clean_orig_name)
        if not sanitized_filename:
            sanitized_filename = "document"

        storage_key = f"documents/{user_id}/{doc_id}/{sanitized_filename}"

        # 7. Upload binary to MinIO
        try:
            self.storage_client.upload_file(
                object_name=storage_key,
                file_data=file_bytes,
                length=file_size,
                content_type=canonical_mime,
            )
        except Exception as exc:
            logger.error("Failed to upload object '%s' to storage: %s", storage_key, exc)
            raise DocumentServiceError(f"Storage upload failed: {exc}") from exc

        # 8. Persist metadata in PostgreSQL
        try:
            document = await self.document_repo.create(
                id=doc_id,
                user_id=user_id,
                course_id=course_id,
                title=clean_title,
                original_file_name=clean_orig_name,
                storage_object_key=storage_key,
                file_type=canonical_mime,
                file_size_bytes=file_size,
                page_count=None,
                status="pending",
            )
        except Exception:
            # Avoid orphaned objects in MinIO on DB error
            logger.error("Database persistence failed for document '%s'; rolling back storage upload", doc_id)
            try:
                self.storage_client.delete_file(storage_key)
            except (StorageError, OSError) as cleanup_err:
                logger.warning("Failed to clean up storage object '%s': %s", storage_key, cleanup_err)
            raise

        return _to_dto(document)

    async def get_document(
        self,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        generate_download_url: bool = True,
    ) -> DocumentDTO:
        """Fetch a document by ID with tenant isolation and optional presigned download URL.

        Args:
            document_id: Document UUID.
            user_id: Authenticated student user UUID.
            generate_download_url: Whether to compute a presigned download URL.

        Returns:
            DocumentDTO with optional download_url.

        Raises:
            DocumentNotFoundError: If document does not exist or is not owned by user_id.
        """
        document = await self.document_repo.get_by_id_and_user(document_id=document_id, user_id=user_id)
        if not document:
            raise DocumentNotFoundError(f"Document '{document_id}' was not found.")

        download_url: str | None = None
        if generate_download_url:
            try:
                download_url = self.storage_client.generate_presigned_download_url(
                    object_name=document.storage_object_key,
                    expires_delta=timedelta(minutes=15),
                    response_headers={
                        "content-disposition": f'attachment; filename="{document.original_file_name}"',
                        "content-type": document.file_type,
                    },
                )
            except (StorageError, OSError) as exc:
                logger.warning(
                    "Could not generate presigned download URL for '%s': %s",
                    document.storage_object_key,
                    exc,
                )
                download_url = None

        return _to_dto(document, download_url=download_url)

    async def list_documents(
        self,
        user_id: uuid.UUID,
        course_id: uuid.UUID | None = None,
        status: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[DocumentDTO], int]:
        """List documents owned by the student with optional course and status filters.

        Args:
            user_id: Authenticated student user UUID.
            course_id: Optional course filter UUID.
            status: Optional processing status filter.
            skip: Pagination offset.
            limit: Pagination limit.

        Returns:
            Tuple of (list of DocumentDTO, total count).
        """
        documents, total = await self.document_repo.list_by_user(
            user_id=user_id,
            course_id=course_id,
            status=status,
            skip=skip,
            limit=limit,
        )
        return [_to_dto(d) for d in documents], total

    async def delete_document(
        self,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Delete a study document from both MinIO storage and PostgreSQL.

        Args:
            document_id: Target document UUID.
            user_id: Authenticated student user UUID.

        Raises:
            DocumentNotFoundError: If document does not exist or is not owned by user_id.
        """
        document = await self.document_repo.get_by_id_and_user(document_id=document_id, user_id=user_id)
        if not document:
            raise DocumentNotFoundError(f"Document '{document_id}' was not found.")

        # Delete object from MinIO
        try:
            self.storage_client.delete_file(document.storage_object_key)
        except (StorageError, OSError) as exc:
            logger.warning(
                "Failed to delete storage object '%s' during document deletion: %s",
                document.storage_object_key,
                exc,
            )

        # Delete metadata row from PostgreSQL
        await self.document_repo.delete(document)


def get_document_service(
    document_repo: Annotated[DocumentRepository, Depends(get_document_repo)],
    course_repo: Annotated[CourseRepository, Depends(get_course_repo)],
    storage_client: Annotated[MinIOStorageClient, Depends(get_storage_client)],
) -> DocumentService:
    """FastAPI dependency provider for DocumentService."""
    return DocumentService(
        document_repo=document_repo,
        course_repo=course_repo,
        storage_client=storage_client,
    )


__all__ = [
    "ALLOWED_EXTENSIONS",
    "ALLOWED_MIME_TYPES",
    "MAX_FILE_SIZE_BYTES",
    "CourseNotFoundError",
    "DocumentDTO",
    "DocumentNotFoundError",
    "DocumentService",
    "DocumentServiceError",
    "DocumentValidationError",
    "FileTooLargeError",
    "UnsupportedFileTypeError",
    "get_document_service",
    "validate_file_type",
]
