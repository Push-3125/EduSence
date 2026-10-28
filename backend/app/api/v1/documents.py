"""Documents API router for university study document uploads and management (US 2.10)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel

from app.core.security import get_current_active_user
from app.services.auth_service import UserDTO
from app.services.document_service import (
    CourseNotFoundError,
    DocumentDTO,
    DocumentNotFoundError,
    DocumentService,
    DocumentServiceError,
    DocumentValidationError,
    FileTooLargeError,
    UnsupportedFileTypeError,
    get_document_service,
)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DocumentData(BaseModel):
    """Data payload representing a study document and its processing metadata."""

    id: uuid.UUID
    user_id: uuid.UUID
    course_id: uuid.UUID | None = None
    title: str
    original_file_name: str
    storage_object_key: str
    file_type: str
    file_size_bytes: int
    page_count: int | None = None
    status: str
    total_chunks: int = 0
    chunks_processed: int = 0
    processing_error_code: str | None = None
    created_at: datetime
    updated_at: datetime
    download_url: str | None = None


class DocumentResponse(BaseModel):
    """Standardized API single document response envelope."""

    success: bool = True
    data: DocumentData
    message: str = "Document operation successful."


class DocumentListData(BaseModel):
    """Data payload for paginated document listing."""

    items: list[DocumentData]
    total: int
    skip: int
    limit: int


class DocumentListResponse(BaseModel):
    """Standardized API list response envelope for documents."""

    success: bool = True
    data: DocumentListData
    message: str = "Documents retrieved successfully."


class DocumentActionResponse(BaseModel):
    """Standardized API response envelope for mutation operations."""

    success: bool = True
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_document_data(dto: DocumentDTO) -> DocumentData:
    """Map domain DocumentDTO to presentation DocumentData schema."""
    return DocumentData(
        id=dto.id,
        user_id=dto.user_id,
        course_id=dto.course_id,
        title=dto.title,
        original_file_name=dto.original_file_name,
        storage_object_key=dto.storage_object_key,
        file_type=dto.file_type,
        file_size_bytes=dto.file_size_bytes,
        page_count=dto.page_count,
        status=dto.status,
        total_chunks=dto.total_chunks,
        chunks_processed=dto.chunks_processed,
        processing_error_code=dto.processing_error_code,
        created_at=dto.created_at,
        updated_at=dto.updated_at,
        download_url=dto.download_url,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/upload",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload study document",
)
@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def upload_document(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
    file: Annotated[
        UploadFile,
        File(description="Document file (PDF, DOCX, PNG, JPEG, WEBP; max 20MB)"),
    ],
    title: Annotated[str | None, Form(description="Optional custom title for the document")] = None,
    course_id: Annotated[uuid.UUID | None, Form(description="Optional associated course UUID")] = None,
) -> DocumentResponse:
    """Upload study document with binary storage in private MinIO and metadata in PostgreSQL.

    Enforces file extension, MIME type, 20 MB size limit, and course tenant ownership.
    """
    try:
        dto = await document_service.upload_document(
            user_id=current_user.id,
            file=file,
            title=title,
            course_id=course_id,
        )
    except UnsupportedFileTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except FileTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except CourseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Course not found.",
        ) from exc
    except DocumentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except DocumentServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return DocumentResponse(
        success=True,
        data=_to_document_data(dto),
        message="Document uploaded successfully.",
    )


@router.get(
    "",
    response_model=DocumentListResponse,
    status_code=status.HTTP_200_OK,
    summary="List authenticated student's documents",
)
async def list_documents(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
    course_id: Annotated[
        uuid.UUID | None,
        Query(description="Filter documents by course UUID"),
    ] = None,
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            pattern="^(pending|processing|embedded|failed)$",
            description="Filter by processing status",
        ),
    ] = None,
    skip: Annotated[int, Query(ge=0, description="Records to skip")] = 0,
    limit: Annotated[int, Query(ge=1, le=100, description="Records to return")] = 50,
) -> DocumentListResponse:
    """List documents belonging strictly to the authenticated student."""
    items, total = await document_service.list_documents(
        user_id=current_user.id,
        course_id=course_id,
        status=status_filter,
        skip=skip,
        limit=limit,
    )
    return DocumentListResponse(
        success=True,
        data=DocumentListData(
            items=[_to_document_data(d) for d in items],
            total=total,
            skip=skip,
            limit=limit,
        ),
        message="Documents retrieved successfully.",
    )


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="Get document metadata and presigned download URL",
)
async def get_document(
    document_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentResponse:
    """Retrieve document metadata and presigned download URL with student tenant isolation."""
    try:
        dto = await document_service.get_document(
            document_id=document_id,
            user_id=current_user.id,
            generate_download_url=True,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        ) from exc

    return DocumentResponse(
        success=True,
        data=_to_document_data(dto),
        message="Document retrieved successfully.",
    )


@router.delete(
    "/{document_id}",
    response_model=DocumentActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Delete study document",
)
async def delete_document(
    document_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentActionResponse:
    """Delete study document from both MinIO storage and PostgreSQL metadata catalog."""
    try:
        await document_service.delete_document(
            document_id=document_id,
            user_id=current_user.id,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        ) from exc

    return DocumentActionResponse(
        success=True,
        message="Document deleted successfully.",
    )
