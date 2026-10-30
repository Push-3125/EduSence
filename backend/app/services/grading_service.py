"""Subjective/Essay Answer Grading and Rubric Evaluation Service (TASK-M5-03 / US 2.15).

Adheres strictly to EduSense Invariants:
- Invariant 2: Router -> Service -> Repository layering.
- Invariant 4: Calls Gemini only through GeminiAdapter.
- Invariant 6: Strict tenant isolation (all operations strictly scoped by user_id).
- Invariant 7: Atomic quota reservation and fail-closed manager before LLM calls.
- Invariant 11: Subjective and essay-answer grading is a REQUIRED core capability.
- Invariant 13: Untrusted context isolated within boundary fences via PromptSanitizer.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from app.providers.gemini_adapter import GeminiAdapter, get_gemini_adapter
from app.providers.usage_logger import AIUsageLogger, get_ai_usage_logger
from app.repositories.practice_repo import (
    PracticeAnswerResult,
    PracticeAttempt,
    PracticeRepository,
    get_practice_repo,
)
from app.repositories.question_repo import (
    Question,
    QuestionRepository,
    get_question_repo,
)
from app.services.quota_service import (
    AIQuotaService,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    get_quota_service,
)
from app.services.sanitizer import PromptSanitizer, get_prompt_sanitizer

logger = logging.getLogger("edusense.services.grading")

__all__ = [
    "AnswerResultDTO",
    "AnswerSubmissionDTO",
    "AttemptAlreadyGradedError",
    "AttemptNotFoundError",
    "AttemptResultDTO",
    "EssayGradingResult",
    "GradingService",
    "GradingServiceError",
    "InvalidSubmissionError",
    "QuestionBankNotFoundError",
    "QuotaExceededError",
    "QuotaServiceUnavailableError",
    "RubricCriterionGrade",
    "get_grading_service",
]


# ---------------------------------------------------------------------------
# Domain Exceptions
# ---------------------------------------------------------------------------


class GradingServiceError(Exception):
    """Base exception for grading service errors."""


class AttemptNotFoundError(GradingServiceError):
    """Raised when target practice attempt does not exist or user lacks ownership."""


class AttemptAlreadyGradedError(GradingServiceError):
    """Raised when target practice attempt is already graded or not in_progress."""


class QuestionBankNotFoundError(GradingServiceError):
    """Raised when target question bank does not exist or user lacks ownership."""


class InvalidSubmissionError(GradingServiceError):
    """Raised when student submission parameters or answers are invalid."""


# ---------------------------------------------------------------------------
# Structured LLM Output Schemas
# ---------------------------------------------------------------------------


class RubricCriterionGrade(BaseModel):
    """Itemized evaluation grade and comment for a specific rubric criterion."""

    criterion_name: str = Field(
        ...,
        description="Name of the rubric criterion (e.g. Content Accuracy, Completeness, Structure).",
    )
    max_points: float = Field(
        ...,
        description="Maximum possible points for this criterion.",
    )
    awarded_points: float = Field(
        ...,
        description="Points awarded for this criterion (must be between 0 and max_points).",
    )
    comment: str = Field(
        ...,
        description="Evaluation feedback and suggestions for improvement.",
    )


class EssayGradingResult(BaseModel):
    """Structured response container returned by Gemini LLM for essay answer grading."""

    score: float = Field(
        ...,
        description="Overall awarded score for the essay.",
    )
    max_score: float = Field(
        ...,
        description="Maximum possible score for the essay.",
    )
    feedback: str = Field(
        ...,
        description="Constructive student-facing feedback summarizing strengths and weaknesses.",
    )
    rationale: str = Field(
        ...,
        description="Detailed academic rationale for the grading decision.",
    )
    rubric_breakdown: list[RubricCriterionGrade] = Field(
        default_factory=list,
        description="Itemized rubric evaluation across criteria.",
    )


# ---------------------------------------------------------------------------
# Domain Data Transfer Objects (DTOs)
# ---------------------------------------------------------------------------


class AnswerSubmissionDTO(BaseModel):
    """DTO representing a single student answer submitted for grading."""

    model_config = ConfigDict(from_attributes=True)

    question_id: uuid.UUID
    answer_text: str = ""


class AnswerResultDTO(BaseModel):
    """DTO representing an evaluated answer result for a single question."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    attempt_id: uuid.UUID
    question_id: uuid.UUID
    question_type: str | None = None
    question_content: str | None = None
    answer_text: str | None = None
    score: float | None = None
    max_score: float | None = None
    feedback: str | None = None
    rationale: str | None = None
    rubric_breakdown: list[dict[str, Any]] | list[RubricCriterionGrade] | None = None
    reference_answer: str | None = None
    grader_model: str | None = None
    graded_at: datetime | None = None

    @classmethod
    def from_orm_model(cls, obj: PracticeAnswerResult) -> AnswerResultDTO:
        """Create DTO from PracticeAnswerResult ORM entity."""
        q_type = obj.question.question_type if obj.question else None
        q_content = obj.question.content if obj.question else None
        return cls(
            id=obj.id,
            attempt_id=obj.attempt_id,
            question_id=obj.question_id,
            question_type=q_type,
            question_content=q_content,
            answer_text=obj.answer_text,
            score=float(obj.score) if obj.score is not None else None,
            max_score=float(obj.max_score) if obj.max_score is not None else None,
            feedback=obj.feedback,
            rationale=obj.rationale,
            rubric_breakdown=obj.rubric_breakdown,
            reference_answer=obj.reference_answer,
            grader_model=obj.grader_model,
            graded_at=obj.graded_at,
        )


class AttemptResultDTO(BaseModel):
    """DTO representing the overall practice attempt status and scores."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    question_bank_id: uuid.UUID
    status: str
    total_score: float | None = None
    max_score: float | None = None
    percentage: float | None = None
    started_at: datetime
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    results: list[AnswerResultDTO] = Field(default_factory=list)

    @classmethod
    def from_orm_model(cls, obj: PracticeAttempt) -> AttemptResultDTO:
        """Create DTO from PracticeAttempt ORM entity."""
        tot = float(obj.total_score) if obj.total_score is not None else None
        mx = float(obj.max_score) if obj.max_score is not None else None
        pct = (
            round((tot / mx) * 100, 2)
            if tot is not None and mx is not None and mx > 0
            else None
        )
        return cls(
            id=obj.id,
            user_id=obj.user_id,
            question_bank_id=obj.question_bank_id,
            status=obj.status,
            total_score=tot,
            max_score=mx,
            percentage=pct,
            started_at=obj.started_at,
            submitted_at=obj.submitted_at,
            graded_at=obj.graded_at,
            results=[
                AnswerResultDTO.from_orm_model(r) for r in (obj.results or [])
            ],
        )


# ---------------------------------------------------------------------------
# Grading Service Implementation
# ---------------------------------------------------------------------------


class GradingService:
    """Service handling objective and subjective evaluation of practice attempts."""

    def __init__(
        self,
        practice_repo: PracticeRepository,
        question_repo: QuestionRepository,
        gemini_adapter: GeminiAdapter,
        quota_service: AIQuotaService,
        usage_logger: AIUsageLogger,
        sanitizer: PromptSanitizer,
    ) -> None:
        self.practice_repo = practice_repo
        self.question_repo = question_repo
        self.gemini_adapter = gemini_adapter
        self.quota_service = quota_service
        self.usage_logger = usage_logger
        self.sanitizer = sanitizer

    async def start_attempt(
        self,
        user_id: uuid.UUID,
        question_bank_id: uuid.UUID,
        max_score: float = 100.0,
    ) -> AttemptResultDTO:
        """Initialize a new practice attempt for a student on a question bank.

        Args:
            user_id: Authenticated student UUID.
            question_bank_id: Target question bank UUID.
            max_score: Maximum achievable score.

        Returns:
            Newly created AttemptResultDTO in 'in_progress' status.

        Raises:
            QuestionBankNotFoundError: If target question bank is not owned by user.
        """
        bank = await self.question_repo.get_bank(
            user_id=user_id, bank_id=question_bank_id
        )
        if bank is None:
            raise QuestionBankNotFoundError(
                f"Question bank {question_bank_id} not found."
            )

        attempt = await self.practice_repo.create_attempt(
            user_id=user_id,
            question_bank_id=question_bank_id,
            max_score=max_score,
        )
        return AttemptResultDTO.from_orm_model(attempt)

    async def get_attempt(
        self,
        user_id: uuid.UUID,
        attempt_id: uuid.UUID,
    ) -> AttemptResultDTO:
        """Fetch attempt details strictly scoped to authenticated user.

        Args:
            user_id: Authenticated student UUID.
            attempt_id: Practice attempt UUID.

        Returns:
            AttemptResultDTO.

        Raises:
            AttemptNotFoundError: If attempt does not exist or user lacks ownership.
        """
        attempt = await self.practice_repo.get_attempt(
            user_id=user_id, attempt_id=attempt_id
        )
        if attempt is None:
            raise AttemptNotFoundError(f"Practice attempt {attempt_id} not found.")
        return AttemptResultDTO.from_orm_model(attempt)

    async def list_attempts(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[AttemptResultDTO], int]:
        """List practice attempts for student with pagination."""
        attempts, total = await self.practice_repo.list_attempts(
            user_id=user_id,
            bank_id=bank_id,
            skip=skip,
            limit=limit,
        )
        return [AttemptResultDTO.from_orm_model(a) for a in attempts], total

    async def grade_attempt(
        self,
        user_id: uuid.UUID,
        attempt_id: uuid.UUID,
        answers: list[AnswerSubmissionDTO],
        plan_code: str = "free",
    ) -> AttemptResultDTO:
        """Evaluate all student answers in a practice attempt and record grades.

        Steps:
        1. Validates attempt exists, belongs to user_id, and is currently 'in_progress'.
        2. Retrieves bank questions.
        3. For each question:
           - Multiple-choice: Deterministic evaluation against correct_answer.
           - Essay: Subjective grading with Gemini structured output and rubric.
        4. Sums scores, verifies total_score <= max_score.
        5. Saves all answer results and updates attempt status to 'graded' in DB.
        6. Returns AttemptResultDTO.

        Args:
            user_id: Authenticated student UUID (strict tenant isolation).
            attempt_id: Target practice attempt UUID.
            answers: List of AnswerSubmissionDTO with student responses.
            plan_code: User subscription tier for quota check ('free' or 'pro').

        Returns:
            AttemptResultDTO with final score and question-level breakdowns.

        Raises:
            AttemptNotFoundError: If attempt does not exist or is not owned by user.
            AttemptAlreadyGradedError: If attempt is not in 'in_progress' state.
            QuotaExceededError: If daily AI quota is exhausted.
            QuotaServiceUnavailableError: If quota service is unreachable.
        """
        # 1. Validate attempt existence and status
        attempt = await self.practice_repo.get_attempt(
            user_id=user_id, attempt_id=attempt_id
        )
        if attempt is None:
            raise AttemptNotFoundError(f"Practice attempt {attempt_id} not found.")

        if attempt.status != "in_progress":
            raise AttemptAlreadyGradedError(
                f"Practice attempt {attempt_id} is in status '{attempt.status}' and cannot be graded again."
            )

        # 2. Retrieve bank questions
        questions: list[Question] = []
        if attempt.bank and attempt.bank.questions:
            questions = list(attempt.bank.questions)
        else:
            bank = await self.question_repo.get_bank(
                user_id=user_id, bank_id=attempt.question_bank_id
            )
            if bank and bank.questions:
                questions = list(bank.questions)

        num_questions = len(questions)
        total_max_score = float(attempt.max_score or 100.0)
        max_score_per_q = (
            round(total_max_score / num_questions, 2)
            if num_questions > 0
            else 0.0
        )

        # Map student answers by question_id
        answers_map: dict[uuid.UUID, str] = {
            a.question_id: a.answer_text.strip() if a.answer_text else ""
            for a in answers
        }

        results_data: list[dict[str, Any]] = []

        # 3. Evaluate each question
        for q in questions:
            student_ans = answers_map.get(q.id, "")

            if q.question_type == "multiple_choice":
                # Deterministic grading
                correct_ans = (q.correct_answer or "").strip()
                explanation = (q.explanation or "").strip()

                student_norm = student_ans.lower()
                correct_norm = correct_ans.lower()

                # Robust matching: exact match, or single-letter choice match (e.g. "a" matches "a. ...")
                is_correct = bool(student_norm) and (
                    student_norm == correct_norm
                    or (
                        len(student_norm) == 1
                        and correct_norm.startswith(
                            (f"{student_norm}.", f"{student_norm})", f"{student_norm} ")
                        )
                    )
                )

                if is_correct:
                    score = max_score_per_q
                    feedback = "Chính xác."
                else:
                    score = 0.0
                    exp_part = f" {explanation}" if explanation else ""
                    feedback = (
                        f"Chưa chính xác. Đáp án đúng là {correct_ans}.{exp_part}"
                    ).strip()

                results_data.append(
                    {
                        "question_id": q.id,
                        "answer_text": student_ans,
                        "score": score,
                        "max_score": max_score_per_q,
                        "feedback": feedback,
                        "rationale": "Chấm điểm tự động cho câu hỏi trắc nghiệm khách quan.",
                        "rubric_breakdown": None,
                        "reference_answer": correct_ans,
                        "grader_model": "deterministic_mcq_evaluator",
                    }
                )

            elif q.question_type == "essay":
                # Subjective essay grading via Gemini
                reference_ans = (q.correct_answer or "").strip()
                explanation = (q.explanation or "").strip()

                # Build rubric criteria distribution
                crit1_pts = round(max_score_per_q * 0.5, 2)
                crit2_pts = round(max_score_per_q * 0.3, 2)
                crit3_pts = round(max_score_per_q - crit1_pts - crit2_pts, 2)

                system_instruction = (
                    "You are an expert university professor and rigorous academic evaluator. "
                    "Your task is to grade a student's subjective essay answer based on the question, reference answer, "
                    "and pedagogical rubric.\n\n"
                    f"The maximum score for this essay question is {max_score_per_q:.2f} points.\n"
                    "Evaluate against three explicit rubric criteria:\n"
                    f"1. Content Accuracy: Correctness of core concepts and factual claims (Max {crit1_pts:.2f} points).\n"
                    f"2. Completeness: Thoroughness, key points covered, and adequate depth (Max {crit2_pts:.2f} points).\n"
                    f"3. Structure: Coherent argumentation, logical organization, and academic clarity (Max {crit3_pts:.2f} points).\n\n"
                    "Constraints:\n"
                    f"- Total awarded score must be between 0.0 and {max_score_per_q:.2f}.\n"
                    "- Sum of awarded_points in rubric_breakdown must equal the overall score.\n"
                    "- Provide clear, constructive feedback in Vietnamese to help the student improve.\n"
                    "- Provide a detailed rationale explaining why specific points were awarded or deducted."
                )

                ref_context = (
                    f"Question: {q.content}\n"
                    f"Reference Solution / Model Answer: {reference_ans}\n"
                    f"Explanation / Rubric Notes: {explanation}"
                )
                student_submission = (
                    student_ans if student_ans.strip() else "[Không có câu trả lời]"
                )
                user_query = (
                    f"Evaluate this student's essay answer:\n{student_submission}"
                )

                # Invariant 13: Untrusted context isolated within boundary fences
                isolated_prompt = self.sanitizer.build_isolated_prompt(
                    system_base=system_instruction,
                    user_query=user_query,
                    retrieved_contexts=[ref_context],
                )

                # Invariant 7: Atomic quota reservation before LLM call
                start_time = time.monotonic()
                async with self.quota_service.reserve(
                    user_id=user_id, plan_code=plan_code
                ):
                    grading_output: EssayGradingResult = (
                        await self.gemini_adapter.agenerate_structured(
                            prompt=isolated_prompt.user_content,
                            response_schema=EssayGradingResult,
                            system_instruction=isolated_prompt.system_instruction,
                            temperature=0.2,
                        )
                    )
                latency_ms = int((time.monotonic() - start_time) * 1000)

                # Invariant 4 & 7: AI telemetry logging (no prompts or user data logged)
                prompt_tokens = max(1, len(isolated_prompt.user_content.split()))
                completion_tokens = max(
                    1, len(str(grading_output.model_dump()).split())
                )
                total_tokens = prompt_tokens + completion_tokens

                await self.usage_logger.log_ai_call(
                    user_id=user_id,
                    feature="grading",
                    model_name=self.gemini_adapter.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    latency_ms=latency_ms,
                    request_status="success",
                )

                # Clamp awarded score strictly between 0 and max_score_per_q (DB constraint guard)
                awarded_score = max(
                    0.0, min(float(grading_output.score), max_score_per_q)
                )

                # Serialize rubric breakdown
                rubric_list = [
                    item.model_dump() if hasattr(item, "model_dump") else dict(item)
                    for item in grading_output.rubric_breakdown
                ]

                results_data.append(
                    {
                        "question_id": q.id,
                        "answer_text": student_ans,
                        "score": awarded_score,
                        "max_score": max_score_per_q,
                        "feedback": grading_output.feedback,
                        "rationale": grading_output.rationale,
                        "rubric_breakdown": rubric_list,
                        "reference_answer": reference_ans,
                        "grader_model": self.gemini_adapter.model,
                    }
                )

            else:
                # Fallback for unexpected question types
                results_data.append(
                    {
                        "question_id": q.id,
                        "answer_text": student_ans,
                        "score": 0.0,
                        "max_score": max_score_per_q,
                        "feedback": "Loại câu hỏi không được hỗ trợ để tự động chấm điểm.",
                        "rationale": f"Question type '{q.question_type}' is unrecognized.",
                        "rubric_breakdown": None,
                        "reference_answer": q.correct_answer,
                        "grader_model": "unsupported_type_fallback",
                    }
                )

        # 4. Calculate total score and enforce DB constraint total_score <= max_score
        raw_total = sum(r["score"] for r in results_data if r.get("score") is not None)
        total_score = max(0.0, min(round(raw_total, 2), total_max_score))

        # 5. Persist to database
        updated_attempt = await self.practice_repo.save_answer_results(
            attempt_id=attempt.id,
            results_data=results_data,
            total_score=total_score,
            status="graded",
        )

        # 6. Return AttemptResultDTO
        return AttemptResultDTO.from_orm_model(updated_attempt)


# ---------------------------------------------------------------------------
# Dependency Provider
# ---------------------------------------------------------------------------


async def get_grading_service(
    practice_repo: Annotated[PracticeRepository, Depends(get_practice_repo)],
    question_repo: Annotated[QuestionRepository, Depends(get_question_repo)],
    gemini_adapter: Annotated[GeminiAdapter, Depends(get_gemini_adapter)],
    quota_service: Annotated[AIQuotaService, Depends(get_quota_service)],
    usage_logger: Annotated[AIUsageLogger, Depends(get_ai_usage_logger)],
    sanitizer: Annotated[PromptSanitizer, Depends(get_prompt_sanitizer)],
) -> GradingService:
    """FastAPI dependency yielding a GradingService instance."""
    return GradingService(
        practice_repo=practice_repo,
        question_repo=question_repo,
        gemini_adapter=gemini_adapter,
        quota_service=quota_service,
        usage_logger=usage_logger,
        sanitizer=sanitizer,
    )
