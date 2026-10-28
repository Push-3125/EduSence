"""Practice Attempts and Score Tracking Service (TASK-M5-04 / US 2.15).

Adheres strictly to EduSense Invariants:
- Invariant 2: Router -> Service -> Repository layering.
- Invariant 6: Strict tenant isolation. Every attempt and score query filters by user_id.
- Invariant 11: Delegates subjective/essay grading to GradingService.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from app.repositories.practice_repo import (
    PracticeAttempt,
    PracticeRepository,
    get_practice_repo,
)
from app.repositories.question_repo import (
    Question,
    QuestionRepository,
    get_question_repo,
)
from app.services.grading_service import (
    AnswerResultDTO,
    AnswerSubmissionDTO,
    AttemptAlreadyGradedError,
    AttemptNotFoundError,
    AttemptResultDTO,
    GradingService,
    InvalidSubmissionError,
    QuestionBankNotFoundError,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    get_grading_service,
)

logger = logging.getLogger("edusense.services.practice")

__all__ = [
    "AnswerResultDTO",
    "AnswerSubmissionDTO",
    "AttemptAlreadyGradedError",
    "AttemptDetailDTO",
    "AttemptNotFoundError",
    "AttemptResultDTO",
    "BankStatisticsDTO",
    "InvalidSubmissionError",
    "PracticeService",
    "QuestionBankNotFoundError",
    "QuotaExceededError",
    "QuotaServiceUnavailableError",
    "StartAttemptDTO",
    "get_practice_service",
]


# ---------------------------------------------------------------------------
# Domain Data Transfer Objects (DTOs)
# ---------------------------------------------------------------------------


class StartAttemptDTO(BaseModel):
    """DTO representing a newly initialized practice attempt."""

    model_config = ConfigDict(from_attributes=True)

    attempt_id: uuid.UUID
    bank_id: uuid.UUID
    bank_name: str
    status: str
    started_at: datetime
    max_score: float
    question_count: int


class AttemptDetailDTO(BaseModel):
    """DTO representing detailed practice attempt with questions and graded results."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    bank_id: uuid.UUID
    bank_name: str
    status: str
    total_score: float | None = None
    max_score: float
    started_at: datetime
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    results: list[AnswerResultDTO] = Field(default_factory=list)

    @classmethod
    def from_orm_model(
        cls, obj: PracticeAttempt, bank_name: str | None = None
    ) -> AttemptDetailDTO:
        """Create AttemptDetailDTO from PracticeAttempt ORM model."""
        resolved_bank_name = bank_name or (
            obj.bank.name if getattr(obj, "bank", None) else ""
        )
        raw_results = getattr(obj, "results", None) or []
        if raw_results:
            results = [AnswerResultDTO.from_orm_model(r) for r in raw_results]
        elif getattr(obj, "bank", None) and getattr(obj.bank, "questions", None):
            questions = list(obj.bank.questions)
            q_cnt = len(questions)
            max_per_q = (
                round(float(obj.max_score or 100.0) / q_cnt, 2)
                if q_cnt > 0
                else 0.0
            )
            results = [
                AnswerResultDTO(
                    id=uuid.uuid4(),
                    attempt_id=obj.id,
                    question_id=q.id,
                    question_type=q.question_type,
                    question_content=q.content,
                    answer_text=None,
                    score=None,
                    max_score=max_per_q,
                    feedback=None,
                    rationale=None,
                    rubric_breakdown=None,
                    reference_answer=None,
                    grader_model=None,
                    graded_at=None,
                )
                for q in questions
            ]
        else:
            results = []

        return cls(
            id=obj.id,
            user_id=obj.user_id,
            bank_id=obj.question_bank_id,
            bank_name=resolved_bank_name,
            status=obj.status,
            total_score=float(obj.total_score) if obj.total_score is not None else None,
            max_score=float(obj.max_score) if obj.max_score is not None else 100.0,
            started_at=obj.started_at,
            submitted_at=obj.submitted_at,
            graded_at=obj.graded_at,
            results=results,
        )


class BankStatisticsDTO(BaseModel):
    """DTO representing aggregate performance metrics for a question bank."""

    model_config = ConfigDict(from_attributes=True)

    bank_id: uuid.UUID
    total_attempts: int
    completed_attempts: int
    average_score: float
    highest_score: float
    max_possible_score: float


# ---------------------------------------------------------------------------
# Practice Service Implementation
# ---------------------------------------------------------------------------


class PracticeService:
    """Service handling practice attempt lifecycles and student score analytics."""

    def __init__(
        self,
        practice_repo: PracticeRepository,
        question_repo: QuestionRepository,
        grading_service: GradingService,
    ) -> None:
        self.practice_repo = practice_repo
        self.question_repo = question_repo
        self.grading_service = grading_service

    async def start_attempt(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID,
    ) -> StartAttemptDTO:
        """Start a new practice attempt for a student on a question bank.

        Args:
            user_id: Authenticated student UUID (strict tenant isolation).
            bank_id: Target question bank UUID.

        Returns:
            StartAttemptDTO containing attempt metadata.

        Raises:
            QuestionBankNotFoundError: If target question bank is not owned by user.
        """
        bank = await self.question_repo.get_bank(user_id=user_id, bank_id=bank_id)
        if bank is None:
            raise QuestionBankNotFoundError(f"Question bank {bank_id} not found.")

        q_count = len(bank.questions) if getattr(bank, "questions", None) else 0
        attempt = await self.practice_repo.create_attempt(
            user_id=user_id,
            question_bank_id=bank_id,
            max_score=100.0,
        )
        return StartAttemptDTO(
            attempt_id=attempt.id,
            bank_id=bank.id,
            bank_name=bank.name,
            status=attempt.status,
            started_at=attempt.started_at,
            max_score=float(attempt.max_score or 100.0),
            question_count=q_count,
        )

    async def get_attempt(
        self,
        user_id: uuid.UUID,
        attempt_id: uuid.UUID,
    ) -> AttemptDetailDTO:
        """Fetch attempt details strictly scoped to authenticated user.

        Args:
            user_id: Authenticated student UUID.
            attempt_id: Practice attempt UUID.

        Returns:
            AttemptDetailDTO with questions and evaluated results.

        Raises:
            AttemptNotFoundError: If attempt does not exist or user lacks ownership.
        """
        attempt = await self.practice_repo.get_attempt(
            user_id=user_id, attempt_id=attempt_id
        )
        if attempt is None:
            raise AttemptNotFoundError(f"Practice attempt {attempt_id} not found.")

        bank_name = attempt.bank.name if getattr(attempt, "bank", None) else ""
        if not bank_name:
            bank = await self.question_repo.get_bank(
                user_id=user_id, bank_id=attempt.question_bank_id
            )
            if bank:
                bank_name = bank.name
                if not getattr(attempt, "bank", None):
                    attempt.bank = bank

        return AttemptDetailDTO.from_orm_model(attempt, bank_name=bank_name)

    async def list_attempts(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[AttemptDetailDTO], int]:
        """List practice attempts for student with pagination and optional bank filter.

        Args:
            user_id: Authenticated student UUID.
            bank_id: Optional question bank filter.
            skip: Pagination offset.
            limit: Maximum items per page.

        Returns:
            Tuple of (list of AttemptDetailDTO, total_count).
        """
        attempts, total_count = await self.practice_repo.list_attempts(
            user_id=user_id,
            bank_id=bank_id,
            skip=skip,
            limit=limit,
        )
        dtos = [AttemptDetailDTO.from_orm_model(a) for a in attempts]
        return dtos, total_count

    async def submit_attempt(
        self,
        user_id: uuid.UUID,
        attempt_id: uuid.UUID,
        answers: list[AnswerSubmissionDTO],
        plan_code: str = "free",
    ) -> AttemptResultDTO:
        """Submit student answers and delegate evaluation to GradingService.

        Args:
            user_id: Authenticated student UUID.
            attempt_id: Practice attempt UUID.
            answers: List of student submitted answers.
            plan_code: Subscription plan tier for AI quota check.

        Returns:
            AttemptResultDTO with final score and question-level breakdowns.

        Raises:
            AttemptNotFoundError: If attempt does not exist or user lacks ownership.
            AttemptAlreadyGradedError: If attempt has already been submitted/graded.
            InvalidSubmissionError: If submitted question IDs are not in the bank.
            QuotaExceededError: If daily AI quota is exhausted.
            QuotaServiceUnavailableError: If quota service is unreachable.
        """
        attempt = await self.practice_repo.get_attempt(
            user_id=user_id, attempt_id=attempt_id
        )
        if attempt is None:
            raise AttemptNotFoundError(f"Practice attempt {attempt_id} not found.")

        if attempt.status != "in_progress":
            raise AttemptAlreadyGradedError(
                f"Practice attempt {attempt_id} is in status '{attempt.status}' and cannot be graded again."
            )

        bank_questions: list[Question] = []
        if getattr(attempt, "bank", None) and getattr(attempt.bank, "questions", None):
            bank_questions = list(attempt.bank.questions)
        else:
            bank = await self.question_repo.get_bank(
                user_id=user_id, bank_id=attempt.question_bank_id
            )
            if bank and getattr(bank, "questions", None):
                bank_questions = list(bank.questions)

        valid_q_ids = {q.id for q in bank_questions}
        for ans in answers:
            if ans.question_id not in valid_q_ids:
                raise InvalidSubmissionError(
                    f"Question {ans.question_id} does not belong to this practice bank."
                )

        return await self.grading_service.grade_attempt(
            user_id=user_id,
            attempt_id=attempt_id,
            answers=answers,
            plan_code=plan_code,
        )

    async def get_bank_statistics(
        self,
        user_id: uuid.UUID,
        bank_id: uuid.UUID,
    ) -> BankStatisticsDTO:
        """Compute aggregate performance metrics across student's attempts for a bank.

        Args:
            user_id: Authenticated student UUID.
            bank_id: Target question bank UUID.

        Returns:
            BankStatisticsDTO with attempts count, completed count, average and highest scores.

        Raises:
            QuestionBankNotFoundError: If question bank does not exist or user lacks ownership.
        """
        bank = await self.question_repo.get_bank(user_id=user_id, bank_id=bank_id)
        if bank is None:
            raise QuestionBankNotFoundError(f"Question bank {bank_id} not found.")

        attempts, total_count = await self.practice_repo.list_attempts(
            user_id=user_id,
            bank_id=bank_id,
            skip=0,
            limit=1000,
        )

        completed_attempts = [
            a
            for a in attempts
            if a.status in ("graded", "submitted") and a.total_score is not None
        ]
        completed_count = len(completed_attempts)

        if completed_count > 0:
            scores = [
                float(a.total_score)
                for a in completed_attempts
                if a.total_score is not None
            ]
            avg_score = round(sum(scores) / completed_count, 2)
            high_score = round(max(scores), 2)
            max_scores = [float(a.max_score or 100.0) for a in completed_attempts]
            max_possible = round(max(max_scores), 2)
        else:
            avg_score = 0.0
            high_score = 0.0
            max_possible = (
                100.0
                if not attempts
                else round(float(attempts[0].max_score or 100.0), 2)
            )

        return BankStatisticsDTO(
            bank_id=bank_id,
            total_attempts=total_count,
            completed_attempts=completed_count,
            average_score=avg_score,
            highest_score=high_score,
            max_possible_score=max_possible,
        )


# ---------------------------------------------------------------------------
# Dependency Provider
# ---------------------------------------------------------------------------


async def get_practice_service(
    practice_repo: Annotated[PracticeRepository, Depends(get_practice_repo)],
    question_repo: Annotated[QuestionRepository, Depends(get_question_repo)],
    grading_service: Annotated[GradingService, Depends(get_grading_service)],
) -> PracticeService:
    """FastAPI dependency yielding a PracticeService instance."""
    return PracticeService(
        practice_repo=practice_repo,
        question_repo=question_repo,
        grading_service=grading_service,
    )
