"""Summaries API router for document AI summarization (US 2.14 / US 2.15).

Adheres strictly to:
- Invariant 2: Router stays thin; delegates all logic to SummaryService. Never imports repositories.
- Invariant 6: Strict tenant isolation (scoped to authenticated user_id).
- Invariant 7: Handles QuotaExceededError (429) and QuotaServiceUnavailableError (503 fail-closed).
- Standard Envelope: { success: true, data: ..., message: ... } for all successful responses.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.core.security import get_current_active_user
from app.providers.gemini_adapter import (
    GeminiCircuitOpenError,
    GeminiRateLimitError,
    GeminiTimeoutError,
)
from app.services.auth_service import UserDTO
from app.services.summary_service import (
    DocumentNotFoundError,
    EmptyDocumentError,
    InvalidSummaryTypeError,
    JobDTO,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    SummaryDTO,
    SummaryNotFoundError,
    SummaryService,
    get_summary_service,
)

router = APIRouter(prefix="/api/v1", tags=["summaries"])


# ---------------------------------------------------------------------------
# Schemas & Response Envelopes
# ---------------------------------------------------------------------------


class SummaryCreateRequest(BaseModel):
    """Optional request body schema for creating a summary."""

    summary_type: str | None = Field(
        default=None,
        description="Depth type of summary ('brief', 'general', 'detailed')",
    )


class SummaryResponse(BaseModel):
    """Standardized API single summary response envelope."""

    success: bool = True
    data: SummaryDTO
    message: str = "Summary generated successfully."


class SummaryListResponse(BaseModel):
    """Standardized API list response envelope for summaries."""

    success: bool = True
    data: list[SummaryDTO]
    message: str = "Summaries retrieved successfully."


class JobResponse(BaseModel):
    """Standardized API response envelope for background jobs."""

    success: bool = True
    data: JobDTO
    message: str = "Summary generation job queued."


class DeleteSummaryResponse(BaseModel):
    """Standardized API response envelope for summary deletion."""

    success: bool = True
    data: Any | None = None
    message: str = "Summary deleted successfully."


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/documents/{document_id}/summaries",
    response_model=SummaryResponse | JobResponse,
    summary="Generate document summary (Sync or Async Job)",
)
async def create_document_summary(
    document_id: uuid.UUID,
    response: Response,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    summary_service: Annotated[SummaryService, Depends(get_summary_service)],
    async_mode: bool = Query(
        False, description="Whether to queue processing in background Celery worker"
    ),
    summary_type: str = Query(
        "general", description="Depth level ('brief', 'general', or 'detailed')"
    ),
    body: SummaryCreateRequest | None = None,
) -> SummaryResponse | JobResponse:
    """Generate or queue an AI-powered summary for a university study document."""
    resolved_type = (body.summary_type if body and body.summary_type else summary_type).strip().lower()

    try:
        if async_mode:
            job = await summary_service.create_async_summary_job(
                user_id=current_user.id,
                document_id=document_id,
                summary_type=resolved_type,
                plan_code=current_user.current_plan_code,
            )
            response.status_code = status.HTTP_202_ACCEPTED
            return JobResponse(
                success=True,
                data=job,
                message="Summary generation job queued.",
            )

        summary = await summary_service.generate_summary(
            user_id=current_user.id,
            document_id=document_id,
            summary_type=resolved_type,
            plan_code=current_user.current_plan_code,
        )
        response.status_code = status.HTTP_201_CREATED
        return SummaryResponse(
            success=True,
            data=summary,
            message="Summary generated successfully.",
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidSummaryTypeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except EmptyDocumentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily AI summarization quota limit reached.",
        ) from exc
    except QuotaServiceUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Quota service temporarily unavailable (fail-closed).",
        ) from exc
    except GeminiRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Google Gemini rate limit reached. Please try again shortly.",
        ) from exc
    except GeminiCircuitOpenError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI service circuit breaker open due to high upstream failure rate.",
        ) from exc
    except GeminiTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="AI generation timed out. Please try again.",
        ) from exc


@router.get(
    "/documents/{document_id}/summaries",
    response_model=SummaryListResponse,
    summary="List all summaries for a study document",
)
async def list_document_summaries(
    document_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    summary_service: Annotated[SummaryService, Depends(get_summary_service)],
) -> SummaryListResponse:
    """Retrieve all previously generated summaries for a study document."""
    try:
        summaries = await summary_service.list_document_summaries(
            user_id=current_user.id,
            document_id=document_id,
        )
        return SummaryListResponse(
            success=True,
            data=summaries,
            message="Summaries retrieved successfully.",
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/summaries/{summary_id}",
    response_model=SummaryResponse,
    summary="Get single summary details",
)
async def get_summary(
    summary_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    summary_service: Annotated[SummaryService, Depends(get_summary_service)],
) -> SummaryResponse:
    """Retrieve full content and metadata of a specific document summary."""
    try:
        summary = await summary_service.get_summary(
            user_id=current_user.id,
            summary_id=summary_id,
        )
        return SummaryResponse(
            success=True,
            data=summary,
            message="Summary retrieved successfully.",
        )
    except SummaryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete(
    "/summaries/{summary_id}",
    response_model=DeleteSummaryResponse,
    summary="Delete a document summary",
)
async def delete_summary(
    summary_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    summary_service: Annotated[SummaryService, Depends(get_summary_service)],
) -> DeleteSummaryResponse:
    """Delete a document summary ensuring strict tenant ownership."""
    try:
        await summary_service.delete_summary(
            user_id=current_user.id,
            summary_id=summary_id,
        )
        return DeleteSummaryResponse(
            success=True,
            data=None,
            message="Summary deleted successfully.",
        )
    except SummaryNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
