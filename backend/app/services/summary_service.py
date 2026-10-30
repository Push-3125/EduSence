"""Document Summarization Service (TASK-M5-01 / US 2.14 / US 2.15).

Adheres strictly to EduSense Invariants:
- Invariant 2: Service coordinates domain logic between repositories and providers.
- Invariant 4: Calls Gemini only through GeminiAdapter.
- Invariant 6: Strict tenant isolation (retrieval and summaries strictly scoped by user_id).
- Invariant 7: Atomic quota reservation and fail-closed manager before LLM calls.
- Invariant 8 & 9: Long AI tasks track status in PostgreSQL background_jobs table.
- Invariant 13: Untrusted context isolated within boundary fences via PromptSanitizer.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from app.providers.gemini_adapter import GeminiAdapter, get_gemini_adapter
from app.providers.usage_logger import AIUsageLogger, get_ai_usage_logger
from app.repositories.chunk_repo import DocumentChunkRepository, get_chunk_repo
from app.repositories.document_repo import DocumentRepository, get_document_repo
from app.repositories.job_repo import BackgroundJob, JobRepository, get_job_repo
from app.repositories.summary_repo import (
    DocumentSummary,
    SummaryRepository,
    get_summary_repo,
)
from app.services.quota_service import (
    AIQuotaService,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    get_quota_service,
)
from app.services.sanitizer import PromptSanitizer, get_prompt_sanitizer

logger = logging.getLogger("edusense.services.summaries")

__all__ = [
    "VALID_SUMMARY_TYPES",
    "DocumentNotFoundError",
    "EmptyDocumentError",
    "InvalidSummaryTypeError",
    "JobDTO",
    "JobNotFoundError",
    "QuotaExceededError",
    "QuotaServiceUnavailableError",
    "SummaryDTO",
    "SummaryNotFoundError",
    "SummaryService",
    "SummaryServiceError",
    "get_summary_service",
]

VALID_SUMMARY_TYPES: set[str] = {"brief", "general", "detailed"}

SUMMARY_SYSTEM_INSTRUCTIONS: dict[str, str] = {
    "brief": (
        "You are an expert academic tutor for EduSense. Produce a concise, high-yield summary "
        "of the provided academic material. Structure your response into 1-2 focused summary "
        "paragraphs explaining the main subject matter, followed by a bulleted list of 3-5 crucial "
        "key takeaways."
    ),
    "general": (
        "You are an expert academic tutor for EduSense. Produce a comprehensive, clear, and "
        "well-balanced general summary of the provided academic material. Structure your response "
        "with an Executive Overview, Major Conceptual Sections with clear explanations, and a "
        "Synthesis Conclusion summarizing key implications."
    ),
    "detailed": (
        "You are an expert academic tutor for EduSense. Produce an in-depth, rigorous, section-by-section "
        "academic breakdown of the provided material. Systematically analyze each topic, explain "
        "definitions, formulas/theories, mechanisms, examples, and highlight nuances, edge cases, "
        "and examination-critical details."
    ),
}


# ---------------------------------------------------------------------------
# Domain Exceptions
# ---------------------------------------------------------------------------


class SummaryServiceError(Exception):
    """Base exception for summarization service errors."""


class DocumentNotFoundError(SummaryServiceError):
    """Raised when target document does not exist or user lacks ownership."""


class InvalidSummaryTypeError(SummaryServiceError):
    """Raised when provided summary type is not supported."""


class EmptyDocumentError(SummaryServiceError):
    """Raised when target document has no text chunks available."""


class SummaryNotFoundError(SummaryServiceError):
    """Raised when requested summary does not exist or user lacks ownership."""


class JobNotFoundError(SummaryServiceError):
    """Raised when requested background job does not exist or user lacks ownership."""


# ---------------------------------------------------------------------------
# Domain Data Transfer Objects (DTOs)
# ---------------------------------------------------------------------------


class SummaryDTO(BaseModel):
    """Data Transfer Object representing a document summary."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    user_id: uuid.UUID
    summary_type: str
    content: str
    model_name: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm(cls, summary: DocumentSummary) -> SummaryDTO:
        """Create SummaryDTO from SQLAlchemy DocumentSummary model."""
        return cls(
            id=summary.id,
            document_id=summary.document_id,
            user_id=summary.user_id,
            summary_type=summary.summary_type,
            content=summary.content,
            model_name=summary.model_name,
            created_at=summary.created_at,
            updated_at=summary.updated_at,
        )


class JobDTO(BaseModel):
    """Data Transfer Object representing a background job."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None = None
    job_type: str = "document_summarization"
    status: str
    progress_percent: int = 0
    result_payload: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_orm(cls, job: BackgroundJob) -> JobDTO:
        """Create JobDTO from SQLAlchemy BackgroundJob model."""
        return cls(
            id=job.id,
            user_id=job.user_id,
            job_type=job.job_type,
            status=job.api_status,
            progress_percent=job.progress_percent,
            result_payload=job.result_payload,
            error_code=job.error_code,
            error_message=job.error_message,
            queued_at=job.queued_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
        )


# ---------------------------------------------------------------------------
# Summary Service Implementation
# ---------------------------------------------------------------------------


class SummaryService:
    """Service orchestrating synchronous and asynchronous document summarization."""

    def __init__(
        self,
        summary_repo: SummaryRepository,
        document_repo: DocumentRepository,
        chunk_repo: DocumentChunkRepository,
        job_repo: JobRepository,
        gemini_adapter: GeminiAdapter,
        usage_logger: AIUsageLogger,
        quota_service: AIQuotaService,
        sanitizer: PromptSanitizer,
    ) -> None:
        self.summary_repo = summary_repo
        self.document_repo = document_repo
        self.chunk_repo = chunk_repo
        self.job_repo = job_repo
        self.gemini_adapter = gemini_adapter
        self.usage_logger = usage_logger
        self.quota_service = quota_service
        self.sanitizer = sanitizer

    async def generate_summary(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        summary_type: str = "general",
        plan_code: str = "free",
    ) -> SummaryDTO:
        """Generate a document summary synchronously using Gemini with quota reservation.

        Args:
            user_id: Student user UUID (tenant isolation).
            document_id: Target document UUID.
            summary_type: Depth type ('brief', 'general', 'detailed').
            plan_code: User subscription tier ('free' or 'pro').

        Returns:
            SummaryDTO containing generated summary content and metadata.

        Raises:
            DocumentNotFoundError: If document not found or access denied.
            InvalidSummaryTypeError: If summary_type is not recognized.
            EmptyDocumentError: If document has no text chunks.
            QuotaExceededError: If daily AI quota is exceeded.
            QuotaServiceUnavailableError: If Redis quota counter is unavailable.
        """
        clean_type = summary_type.strip().lower()
        if clean_type not in VALID_SUMMARY_TYPES:
            raise InvalidSummaryTypeError(
                f"Invalid summary type '{summary_type}'. Must be one of: {', '.join(sorted(VALID_SUMMARY_TYPES))}"
            )

        # 1. Enforce strict document ownership
        doc = await self.document_repo.get_by_id_and_user(document_id, user_id)
        if not doc:
            raise DocumentNotFoundError(
                f"Document {document_id} not found or access denied for user {user_id}."
            )

        # 2. Collect document chunks
        chunks = await self.chunk_repo.get_chunks_by_document(document_id)
        valid_chunks = [c.content for c in chunks if c.content and c.content.strip()]
        if not valid_chunks:
            raise EmptyDocumentError(
                f"Document {document_id} has no text content available for summarization."
            )

        # 3. Build isolated prompt with boundary fences (Invariant 13)
        system_base = SUMMARY_SYSTEM_INSTRUCTIONS[clean_type]
        isolated_prompt = self.sanitizer.build_isolated_prompt(
            system_base=system_base,
            user_query=f"Please provide a {clean_type} academic summary of this document.",
            retrieved_contexts=valid_chunks,
        )

        # 4. Atomic quota reservation with auto-refund on failure (Invariant 7)
        start_time = time.monotonic()
        async with self.quota_service.reserve(user_id=user_id, plan_code=plan_code):
            response = await self.gemini_adapter.agenerate_text(
                prompt=isolated_prompt.user_content,
                system_instruction=isolated_prompt.system_instruction,
            )

        latency_ms = int((time.monotonic() - start_time) * 1000)

        # 5. Persist to document_summaries
        summary = await self.summary_repo.create_summary(
            document_id=document_id,
            user_id=user_id,
            summary_type=clean_type,
            content=response.text,
            model_name=response.model,
        )

        # 6. Record telemetry (Invariant 4 & 7: no prompts logged)
        await self.usage_logger.log_ai_call(
            user_id=user_id,
            feature="summary",
            model_name=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
            latency_ms=latency_ms,
            request_status="success",
        )

        return SummaryDTO.from_orm(summary)

    async def create_async_summary_job(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        summary_type: str = "general",
        plan_code: str = "free",
    ) -> JobDTO:
        """Create a background job and dispatch Celery worker task for async summarization.

        Args:
            user_id: Student user UUID (tenant isolation).
            document_id: Target document UUID.
            summary_type: Depth type ('brief', 'general', 'detailed').
            plan_code: User subscription tier ('free' or 'pro').

        Returns:
            JobDTO in 'queued' status.

        Raises:
            DocumentNotFoundError: If document not found or access denied.
            InvalidSummaryTypeError: If summary_type is not recognized.
        """
        clean_type = summary_type.strip().lower()
        if clean_type not in VALID_SUMMARY_TYPES:
            raise InvalidSummaryTypeError(
                f"Invalid summary type '{summary_type}'. Must be one of: {', '.join(sorted(VALID_SUMMARY_TYPES))}"
            )

        # Enforce strict document ownership
        doc = await self.document_repo.get_by_id_and_user(document_id, user_id)
        if not doc:
            raise DocumentNotFoundError(
                f"Document {document_id} not found or access denied for user {user_id}."
            )

        # Create BackgroundJob record
        job = await self.job_repo.create_job(
            user_id=user_id,
            job_type="document_summarization",
        )

        # Dispatch Celery worker task
        try:
            from app.worker.tasks.summary_tasks import generate_summary_task

            generate_summary_task.delay(
                str(job.id),
                str(document_id),
                str(user_id),
                clean_type,
                plan_code,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Celery dispatch exception (e.g. running in eager/test mode): %s", exc)

        return JobDTO.from_orm(job)

    async def get_job_status(
        self,
        user_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> JobDTO:
        """Fetch background job status enforcing strict tenant isolation.

        Args:
            user_id: Student user UUID.
            job_id: Job UUID.

        Returns:
            JobDTO with current status and results.

        Raises:
            JobNotFoundError: If job not found or not owned by user_id.
        """
        job = await self.job_repo.get_job(user_id=user_id, job_id=job_id)
        if not job:
            raise JobNotFoundError(f"Background job {job_id} not found.")
        return JobDTO.from_orm(job)

    async def list_document_summaries(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> list[SummaryDTO]:
        """List all summaries for a document scoped to authenticated student.

        Args:
            user_id: Student user UUID.
            document_id: Document UUID.

        Returns:
            List of SummaryDTO instances.

        Raises:
            DocumentNotFoundError: If document not found or access denied.
        """
        doc = await self.document_repo.get_by_id_and_user(document_id, user_id)
        if not doc:
            raise DocumentNotFoundError(
                f"Document {document_id} not found or access denied for user {user_id}."
            )

        summaries = await self.summary_repo.list_summaries_by_document(
            user_id=user_id,
            document_id=document_id,
        )
        return [SummaryDTO.from_orm(s) for s in summaries]

    async def get_summary(
        self,
        user_id: uuid.UUID,
        summary_id: uuid.UUID,
    ) -> SummaryDTO:
        """Fetch a specific summary by ID ensuring tenant ownership.

        Args:
            user_id: Student user UUID.
            summary_id: Summary UUID.

        Returns:
            SummaryDTO instance.

        Raises:
            SummaryNotFoundError: If summary not found or access denied.
        """
        summary = await self.summary_repo.get_summary(user_id=user_id, summary_id=summary_id)
        if not summary:
            raise SummaryNotFoundError(f"Summary {summary_id} not found.")
        return SummaryDTO.from_orm(summary)

    async def delete_summary(
        self,
        user_id: uuid.UUID,
        summary_id: uuid.UUID,
    ) -> None:
        """Delete a summary ensuring tenant ownership.

        Args:
            user_id: Student user UUID.
            summary_id: Summary UUID.

        Raises:
            SummaryNotFoundError: If summary not found or access denied.
        """
        deleted = await self.summary_repo.delete_summary(user_id=user_id, summary_id=summary_id)
        if not deleted:
            raise SummaryNotFoundError(f"Summary {summary_id} not found.")


# ---------------------------------------------------------------------------
# Dependency Provider
# ---------------------------------------------------------------------------


async def get_summary_service(
    summary_repo: Annotated[SummaryRepository, Depends(get_summary_repo)],
    document_repo: Annotated[DocumentRepository, Depends(get_document_repo)],
    chunk_repo: Annotated[DocumentChunkRepository, Depends(get_chunk_repo)],
    job_repo: Annotated[JobRepository, Depends(get_job_repo)],
    gemini_adapter: Annotated[GeminiAdapter, Depends(get_gemini_adapter)],
    usage_logger: Annotated[AIUsageLogger, Depends(get_ai_usage_logger)],
    quota_service: Annotated[AIQuotaService, Depends(get_quota_service)],
    sanitizer: Annotated[PromptSanitizer, Depends(get_prompt_sanitizer)],
) -> SummaryService:
    """FastAPI dependency provider for SummaryService."""
    return SummaryService(
        summary_repo=summary_repo,
        document_repo=document_repo,
        chunk_repo=chunk_repo,
        job_repo=job_repo,
        gemini_adapter=gemini_adapter,
        usage_logger=usage_logger,
        quota_service=quota_service,
        sanitizer=sanitizer,
    )
