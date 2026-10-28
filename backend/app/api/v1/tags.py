"""Tags API router for managing user-scoped study document tags (US 2.11)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.v1.documents import (
    TagCreateRequest,
    TagData,
    TagListData,
    TagListResponse,
    TagResponse,
)
from app.core.security import get_current_active_user
from app.services.auth_service import UserDTO
from app.services.document_service import (
    DocumentService,
    DocumentValidationError,
    TagAlreadyExistsError,
    TagNotFoundError,
    get_document_service,
)

router = APIRouter(prefix="/api/v1/tags", tags=["tags"])


class TagActionResponse(BaseModel):
    """Standardized API response envelope for tag mutation operations."""

    success: bool = True
    message: str


@router.get(
    "",
    response_model=TagListResponse,
    status_code=status.HTTP_200_OK,
    summary="List authenticated student's tags",
)
async def list_tags(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> TagListResponse:
    """List all study document tags belonging to the student with document counts."""
    tag_dtos = await document_service.list_tags(user_id=current_user.id)
    items = [
        TagData(
            id=dto.id,
            name=dto.name,
            document_count=dto.document_count,
            created_at=dto.created_at,
        )
        for dto in tag_dtos
    ]
    return TagListResponse(
        success=True,
        data=TagListData(items=items, total=len(items)),
        message="Tags retrieved successfully.",
    )


@router.post(
    "",
    response_model=TagResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new tag",
)
async def create_tag(
    payload: TagCreateRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> TagResponse:
    """Create a new personal tag for organizing study documents."""
    try:
        dto = await document_service.create_tag(
            user_id=current_user.id,
            name=payload.name,
        )
    except TagAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A tag with this name already exists.",
        ) from exc
    except DocumentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return TagResponse(
        success=True,
        data=TagData(
            id=dto.id,
            name=dto.name,
            document_count=dto.document_count,
            created_at=dto.created_at,
        ),
        message="Tag created successfully.",
    )


@router.delete(
    "/{tag_id}",
    response_model=TagActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Delete a tag",
)
async def delete_tag(
    tag_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> TagActionResponse:
    """Delete a personal tag owned by the student."""
    try:
        await document_service.delete_tag(
            user_id=current_user.id,
            tag_id=tag_id,
        )
    except TagNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tag not found.",
        ) from exc

    return TagActionResponse(
        success=True,
        message="Tag deleted successfully.",
    )
