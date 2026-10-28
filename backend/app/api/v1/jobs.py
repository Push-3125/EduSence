"""Background jobs API router for async task status polling (Invariant 8 & 9).

Adheres strictly to:
- Invariant 2: Router stays thin; delegates all logic to SummaryService. Never imports repositories.
- Invariant 6: Strict tenant isolation (users can only poll their own background jobs).
- Standard Envelope: { success: true, data: ..., message: ... } for all successful responses.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.security import get_current_active_user
from app.services.auth_service import UserDTO
from app.services.summary_service import (
    JobDTO,
    JobNotFoundError,
    SummaryService,
    get_summary_service,
)

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


class JobStatusResponse(BaseModel):
    """Standardized API response envelope for background job status."""

    success: bool = True
    data: JobDTO
    message: str = "Job status retrieved successfully."


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    summary="Poll status of a background job",
)
async def get_job_status(
    job_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    summary_service: Annotated[SummaryService, Depends(get_summary_service)],
) -> JobStatusResponse:
    """Retrieve the current execution status and result payload of a background job."""
    try:
        job = await summary_service.get_job_status(
            user_id=current_user.id,
            job_id=job_id,
        )
        return JobStatusResponse(
            success=True,
            data=job,
            message="Job status retrieved successfully.",
        )
    except JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
