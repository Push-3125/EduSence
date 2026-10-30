"""Question Banks API router for practice questions and automated question generation (US 2.15 / US 2.16).

Adheres strictly to:
- Invariant 2: Router stays thin; delegates all logic to QuestionGenerationService. Never imports repositories.
- Invariant 6: Strict tenant isolation (scoped to authenticated user_id).
- Invariant 7: Handles QuotaExceededError (429) and QuotaServiceUnavailableError (503 fail-closed).
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
from app.services.question_generation_service import (
    DocumentNotFoundError,
    EmptyDocumentError,
    InvalidQuestionConfigError,
    QuestionBankDTO,
    QuestionBankNotFoundError,
    QuestionDTO,
    QuestionGenerationService,
    QuestionNotFoundError,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    get_question_service,
)

router = APIRouter(prefix="/api/v1", tags=["question-banks"])


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------


class GenerateQuestionsRequest(BaseModel):
    """Request payload for automated question generation from a document."""

    document_id: uuid.UUID
    num_questions: int = Field(
        default=5, ge=1, le=20, description="Number of questions to generate (1-20)"
    )
    question_type: str = Field(
        default="multiple_choice",
        description="Type of questions ('multiple_choice', 'essay', or 'mixed')",
    )
    difficulty: str = Field(
        default="medium",
        description="Difficulty level ('easy', 'medium', or 'hard')",
    )


class CreateQuestionBankRequest(BaseModel):
    """Request payload for manually creating an empty question bank."""

    name: str = Field(..., min_length=1, max_length=255, description="Bank title")
    description: str | None = Field(default=None, description="Optional description")
    course_id: uuid.UUID | None = Field(default=None, description="Optional linked course UUID")
    document_id: uuid.UUID | None = Field(default=None, description="Optional linked document UUID")


class CreateQuestionRequest(BaseModel):
    """Request payload for manually adding a question to a bank."""

    question_type: str = Field(
        ..., description="Question type ('multiple_choice' or 'essay')"
    )
    content: str = Field(..., min_length=1, description="Question text prompt")
    options: list[str] | None = Field(
        default=None, description="Array of choices (required for multiple_choice)"
    )
    correct_answer: str | None = Field(default=None, description="Correct answer text")
    explanation: str | None = Field(default=None, description="Pedagogical explanation")
    difficulty: str = Field(
        default="medium", description="Difficulty ('easy', 'medium', or 'hard')"
    )


class QuestionBankResponse(BaseModel):
    """Standardized single question bank response envelope."""

    success: bool = True
    data: QuestionBankDTO
    message: str = "Question bank retrieved successfully."


class QuestionBankListResponse(BaseModel):
    """Standardized question bank list response envelope."""

    success: bool = True
    data: list[QuestionBankDTO]
    message: str = "Question banks retrieved successfully."
    meta: dict[str, Any] | None = None


class QuestionResponse(BaseModel):
    """Standardized single question response envelope."""

    success: bool = True
    data: QuestionDTO
    message: str = "Question retrieved successfully."


class DeleteResponse(BaseModel):
    """Standardized deletion response envelope."""

    success: bool = True
    data: Any | None = None
    message: str = "Resource deleted successfully."


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/question-banks/generate",
    response_model=QuestionBankResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate question bank from document using AI",
)
async def generate_question_bank(
    body: GenerateQuestionsRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    question_service: Annotated[QuestionGenerationService, Depends(get_question_service)],
) -> QuestionBankResponse:
    """Generate multiple-choice, essay, or mixed questions from study document text."""
    try:
        bank = await question_service.generate_questions_from_document(
            user_id=current_user.id,
            document_id=body.document_id,
            num_questions=body.num_questions,
            question_type=body.question_type,
            difficulty=body.difficulty,
            plan_code=current_user.current_plan_code,
        )
        return QuestionBankResponse(
            success=True,
            data=bank,
            message="Question bank generated successfully.",
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except EmptyDocumentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvalidQuestionConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily AI question generation quota limit reached.",
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
            detail="AI service timed out while generating questions.",
        ) from exc


@router.post(
    "/question-banks",
    response_model=QuestionBankResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an empty question bank manually",
)
async def create_question_bank(
    body: CreateQuestionBankRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    question_service: Annotated[QuestionGenerationService, Depends(get_question_service)],
) -> QuestionBankResponse:
    """Create an empty question bank for organizing practice questions."""
    try:
        bank = await question_service.create_bank(
            user_id=current_user.id,
            name=body.name,
            description=body.description,
            course_id=body.course_id,
            document_id=body.document_id,
        )
        return QuestionBankResponse(
            success=True,
            data=bank,
            message="Question bank created successfully.",
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidQuestionConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get(
    "/question-banks",
    response_model=QuestionBankListResponse,
    summary="List student's question banks",
)
async def list_question_banks(
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    question_service: Annotated[QuestionGenerationService, Depends(get_question_service)],
    skip: Annotated[int, Query(ge=0, description="Offset items")] = 0,
    limit: Annotated[int, Query(ge=1, le=100, description="Limit items per page")] = 50,
    course_id: Annotated[uuid.UUID | None, Query(description="Optional course filter")] = None,
) -> QuestionBankListResponse:
    """Retrieve all question banks owned by the authenticated student."""
    banks, total = await question_service.list_banks(
        user_id=current_user.id,
        skip=skip,
        limit=limit,
        course_id=course_id,
    )
    return QuestionBankListResponse(
        success=True,
        data=banks,
        message="Question banks retrieved successfully.",
        meta={"total": total, "skip": skip, "limit": limit},
    )


@router.get(
    "/question-banks/{bank_id}",
    response_model=QuestionBankResponse,
    summary="Get question bank details with questions",
)
async def get_question_bank(
    bank_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    question_service: Annotated[QuestionGenerationService, Depends(get_question_service)],
) -> QuestionBankResponse:
    """Retrieve a single question bank by ID with all its questions."""
    try:
        bank = await question_service.get_bank(
            user_id=current_user.id,
            bank_id=bank_id,
        )
        return QuestionBankResponse(
            success=True,
            data=bank,
            message="Question bank retrieved successfully.",
        )
    except QuestionBankNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete(
    "/question-banks/{bank_id}",
    response_model=DeleteResponse,
    summary="Delete a question bank and its questions",
)
async def delete_question_bank(
    bank_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    question_service: Annotated[QuestionGenerationService, Depends(get_question_service)],
) -> DeleteResponse:
    """Delete a question bank and cascade all associated questions."""
    try:
        await question_service.delete_bank(
            user_id=current_user.id,
            bank_id=bank_id,
        )
        return DeleteResponse(
            success=True,
            data=None,
            message="Question bank deleted successfully.",
        )
    except QuestionBankNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/question-banks/{bank_id}/questions",
    response_model=QuestionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a question manually to a question bank",
)
async def add_question_to_bank(
    bank_id: uuid.UUID,
    body: CreateQuestionRequest,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    question_service: Annotated[QuestionGenerationService, Depends(get_question_service)],
) -> QuestionResponse:
    """Manually add a multiple-choice or essay question to an existing bank."""
    try:
        question = await question_service.add_question(
            user_id=current_user.id,
            bank_id=bank_id,
            question_type=body.question_type,
            content=body.content,
            options=body.options,
            correct_answer=body.correct_answer,
            explanation=body.explanation,
            difficulty=body.difficulty,
        )
        return QuestionResponse(
            success=True,
            data=question,
            message="Question added successfully.",
        )
    except QuestionBankNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidQuestionConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.delete(
    "/question-banks/{bank_id}/questions/{question_id}",
    response_model=DeleteResponse,
    summary="Delete a question from a question bank",
)
async def delete_question_from_bank(
    bank_id: uuid.UUID,
    question_id: uuid.UUID,
    current_user: Annotated[UserDTO, Depends(get_current_active_user)],
    question_service: Annotated[QuestionGenerationService, Depends(get_question_service)],
) -> DeleteResponse:
    """Delete a question from a bank, verifying ownership."""
    try:
        await question_service.delete_question(
            user_id=current_user.id,
            bank_id=bank_id,
            question_id=question_id,
        )
        return DeleteResponse(
            success=True,
            data=None,
            message="Question deleted successfully.",
        )
    except QuestionBankNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except QuestionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
