"""Celery background worker tasks for asynchronous document summarization.

Adheres strictly to EduSense Invariants:
- Invariant 4: Calls Gemini only through GeminiAdapter.
- Invariant 6: Strict tenant isolation (user_id and document_id scoping).
- Invariant 7: Atomic quota reservation and fail-closed manager.
- Invariant 8: Every worker task passes through the Idempotency Guard before execution.
- Invariant 9: Tasks pass IDs, not large payloads. Set max_retries with backoff.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from app.core.database import async_session_factory
from app.providers.gemini_adapter import get_gemini_adapter
from app.providers.usage_logger import AIUsageLogger
from app.repositories.ai_usage_repo import AIUsageRepository
from app.repositories.chunk_repo import DocumentChunkRepository
from app.repositories.document_repo import DocumentRepository
from app.repositories.job_repo import JobRepository
from app.repositories.summary_repo import SummaryRepository
from app.services.quota_service import get_quota_service
from app.services.sanitizer import get_prompt_sanitizer
from app.services.summary_service import SummaryService
from app.worker.celery_app import celery_app
from app.worker.idempotency import idempotent_task

logger = logging.getLogger("edusense.worker.summaries")


@celery_app.task(
    name="app.worker.tasks.summary.generate_summary",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
@idempotent_task(task_name="document_summarization", key_param="job_id")
def generate_summary_task(
    self: Any,
    job_id: str,
    document_id: str,
    user_id: str,
    summary_type: str = "general",
    plan_code: str = "free",
) -> dict[str, Any]:
    """Execute asynchronous document summarization pipeline wrapped in Idempotency Guard.

    Args:
        self: Bound Celery task instance.
        job_id: UUID string of the BackgroundJob record.
        document_id: UUID string of the document to summarize.
        user_id: UUID string of the student owner.
        summary_type: Depth type ('brief', 'general', 'detailed').
        plan_code: Subscription tier ('free' or 'pro').

    Returns:
        Dictionary containing job_id, status, and summary_id.
    """
    job_uuid = uuid.UUID(job_id)
    doc_uuid = uuid.UUID(document_id)
    user_uuid = uuid.UUID(user_id)

    async def _execute_summary() -> dict[str, Any]:
        async with async_session_factory() as session:
            job_repo = JobRepository(session)
            summary_repo = SummaryRepository(session)
            doc_repo = DocumentRepository(session)
            chunk_repo = DocumentChunkRepository(session)
            usage_repo = AIUsageRepository(session)

            gemini_adapter = get_gemini_adapter()
            usage_logger = AIUsageLogger(repository=usage_repo)
            quota_service = get_quota_service()
            sanitizer = get_prompt_sanitizer()

            service = SummaryService(
                summary_repo=summary_repo,
                document_repo=doc_repo,
                chunk_repo=chunk_repo,
                job_repo=job_repo,
                gemini_adapter=gemini_adapter,
                usage_logger=usage_logger,
                quota_service=quota_service,
                sanitizer=sanitizer,
            )

            # 1. Update job to running, 20%
            await job_repo.update_job_status(
                job_id=job_uuid,
                status="running",
                progress_percent=20,
            )
            await session.commit()

            try:
                summary_dto = await service.generate_summary(
                    user_id=user_uuid,
                    document_id=doc_uuid,
                    summary_type=summary_type,
                    plan_code=plan_code,
                )

                # 2. Update job to completed, 100% with summary_id in result_payload
                await job_repo.update_job_status(
                    job_id=job_uuid,
                    status="completed",
                    progress_percent=100,
                    result_payload={"summary_id": str(summary_dto.id)},
                )
                await session.commit()
                return {
                    "job_id": job_id,
                    "status": "completed",
                    "summary_id": str(summary_dto.id),
                }
            except Exception as exc:
                error_code = type(exc).__name__
                error_message = str(exc)
                logger.exception("Summary generation task failed for job %s", job_id)
                await job_repo.update_job_status(
                    job_id=job_uuid,
                    status="failed",
                    progress_percent=0,
                    error_code=error_code,
                    error_message=error_message,
                )
                await session.commit()
                raise

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    return loop.run_until_complete(_execute_summary())
