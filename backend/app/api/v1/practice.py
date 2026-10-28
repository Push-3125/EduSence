"""Practice Attempts and Score Tracking API Router (TASK-M5-04 / US 2.15).

Adheres strictly to:
- Invariant 2: Router stays thin; delegates all logic to PracticeService. Never imports repositories.
- Invariant 6: Strict tenant isolation (scoped to authenticated user_id).
- Invariant 11: Subjective and essay-answer grading via GradingService.
- Standard Envelope: { success: true, data: ..., message: ... } for all successful responses.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.security import get_current_active_user
from app.providers.gemini_adapter import (
    GeminiCircuitOpenError,
    GeminiRateLimitError,
    GeminiTimeoutError,
)
from app.services.auth_service import UserDTO
from app.services.grading_service import (
    AnswerSubmissionDTO,
    AttemptAlreadyGradedError,
    AttemptNotFoundError,
    AttemptResultDTO,
    InvalidSubmissionError,
    QuestionBankNotFoundError,
    QuotaExceededError,
    QuotaServiceUnavailableError,
)
from app.services.practice_service import (
    AttemptDetailDTO,
    BankStatisticsDTO,
    PracticeService,
    StartAttemptDTO,
    get_practice_service,
)

router = APIRouter(prefix="/api/v1/practice", tags=["practice"])


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------


class StartAttemptResponse(BaseModel):
    """Standard envelope for starting a practice attempt."""

    success: bool = True
    data: StartAttemptDTO
    message: str = "Practice attempt started successfully."


class AttemptListResponse(BaseModel):
    """Standard envelope for listing practice attempts."""

    success: bool = True
    data: list[AttemptDetailDTO]
    message: str = "Practice attempts retrieved successfully."
    meta: dict[str, Any] | None = None


class AttemptDetailResponse(BaseModel):
    """Standard envelope for fetching a single practice attempt detail."""

    success: bool = True
    data: AttemptDetailDTO
    message: str = "Practice attempt retrieved successfully."


class SubmitAttemptRequest(BaseModel):
    """Request payload for submitting answers to a practice attempt."""

    answers: list[AnswerSubmissionDTO] = Field(
        default_factory=list,
        description="List of student answers submitted for evaluation",
    )


class SubmitAttemptResponse(BaseModel):
    """Standard envelope for submitting and grading a practice attempt."""

    success: bool = True
    data: AttemptResultDTO
    message: str = "Practice attempt submitted and graded successfully."


class BankStatisticsResponse(BaseModel):
    """Standard envelope for question bank practice statistics."""

    success: bool = True
    data: BankStatisticsDTO
    message: str = "Bank practice statistics retrieved successfully."


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/banks/{bank_id}/attempts",
    response_model=StartAttemptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new practice attempt",
)
async def start_attempt(
    bank_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    practice_service: Annotated[PracticeService, Depends(get_practice_service)],
) -> StartAttemptResponse:
    """Initialize a new practice attempt on a question bank."""
    try:
        attempt_dto = await practice_service.start_attempt(
            user_id=current_user.id,
            bank_id=bank_id,
        )
        return StartAttemptResponse(
            success=True,
            data=attempt_dto,
            message="Practice attempt started successfully.",
        )
    except QuestionBankNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/attempts",
    response_model=AttemptListResponse,
    summary="List student's practice attempts",
)
async def list_attempts(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    practice_service: Annotated[PracticeService, Depends(get_practice_service)],
    bank_id: Annotated[uuid.UUID | None, Query(description="Filter by question bank UUID")] = None,
    skip: Annotated[int, Query(ge=0, description="Offset items")] = 0,
    limit: Annotated[int, Query(ge=1, le=100, description="Limit items per page")] = 50,
) -> AttemptListResponse:
    """Retrieve practice attempts for the authenticated student."""
    attempts, total = await practice_service.list_attempts(
        user_id=current_user.id,
        bank_id=bank_id,
        skip=skip,
        limit=limit,
    )
    return AttemptListResponse(
        success=True,
        data=attempts,
        message="Practice attempts retrieved successfully.",
        meta={"total": total, "skip": skip, "limit": limit},
    )


@router.get(
    "/attempts/{attempt_id}",
    response_model=AttemptDetailResponse,
    summary="Get practice attempt details with questions and results",
)
async def get_attempt(
    attempt_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    practice_service: Annotated[PracticeService, Depends(get_practice_service)],
) -> AttemptDetailResponse:
    """Retrieve details, questions, and evaluated results for a practice attempt."""
    try:
        attempt = await practice_service.get_attempt(
            user_id=current_user.id,
            attempt_id=attempt_id,
        )
        return AttemptDetailResponse(
            success=True,
            data=attempt,
            message="Practice attempt retrieved successfully.",
        )
    except AttemptNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/attempts/{attempt_id}/submit",
    response_model=SubmitAttemptResponse,
    summary="Submit answers and receive graded evaluation",
)
async def submit_attempt(
    attempt_id: uuid.UUID,
    body: SubmitAttemptRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    practice_service: Annotated[PracticeService, Depends(get_practice_service)],
) -> SubmitAttemptResponse:
    """Submit student answers to an attempt, evaluate objective & essay questions, and return score."""
    try:
        result = await practice_service.submit_attempt(
            user_id=current_user.id,
            attempt_id=attempt_id,
            answers=body.answers,
            plan_code=current_user.current_plan_code,
        )
        return SubmitAttemptResponse(
            success=True,
            data=result,
            message="Practice attempt submitted and graded successfully.",
        )
    except AttemptNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AttemptAlreadyGradedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InvalidSubmissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily AI grading quota limit reached.",
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
            detail="AI service timed out while grading answers.",
        ) from exc


@router.get(
    "/banks/{bank_id}/statistics",
    response_model=BankStatisticsResponse,
    summary="Get question bank practice statistics",
)
async def get_bank_statistics(
    bank_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    practice_service: Annotated[PracticeService, Depends(get_practice_service)],
) -> BankStatisticsResponse:
    """Retrieve aggregate practice performance metrics for a question bank."""
    try:
        stats = await practice_service.get_bank_statistics(
            user_id=current_user.id,
            bank_id=bank_id,
        )
        return BankStatisticsResponse(
            success=True,
            data=stats,
            message="Bank practice statistics retrieved successfully.",
        )
    except QuestionBankNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
