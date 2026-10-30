"""Celery background worker tasks for document text extraction, chunking, and pgvector embedding.

Adheres strictly to EduSense Invariants:
- Invariant 4: Embeddings called exclusively through shared EmbeddingAdapter provider.
- Invariant 6: Strict tenant isolation (retrieval/processing strictly scoped by user_id).
- Invariant 8: Every worker task passes through the Idempotency Guard before execution.
- Invariant 9: Tasks pass IDs, not large payloads. Set max_retries with backoff.
"""

import asyncio
import logging
import uuid
from typing import Any

from app.clients.storage_client import get_storage_client
from app.core.database import async_session_factory
from app.providers.embedding_adapter import get_embedding_adapter
from app.repositories.chunk_repo import DocumentChunkRepository
from app.repositories.course_repo import CourseRepository
from app.repositories.document_repo import DocumentRepository
from app.services.chunking_service import get_chunking_service
from app.services.document_service import DocumentService
from app.services.extraction_service import get_extraction_service
from app.worker.celery_app import celery_app
from app.worker.idempotency import idempotent_task

logger = logging.getLogger("edusense.worker.documents")


@celery_app.task(
    name="app.worker.tasks.document.ingest_document",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
@idempotent_task(task_name="document_ingestion", key_param="document_id")
def ingest_document_task(self: Any, document_id: str, user_id: str) -> dict[str, str]:
    """Execute asynchronous document ingestion pipeline wrapped in Idempotency Guard.

    Args:
        document_id: UUID string of the document to ingest.
        user_id: UUID string of the student owner (strict tenant isolation).

    Returns:
        Dict with status and document_id.
    """
    doc_uuid = uuid.UUID(document_id)
    user_uuid = uuid.UUID(user_id)

    async def _execute_ingestion() -> str:
        async with async_session_factory() as session:
            doc_repo = DocumentRepository(session)
            course_repo = CourseRepository(session)
            chunk_repo = DocumentChunkRepository(session)
            storage_client = get_storage_client()
            embedding_adapter = get_embedding_adapter()
            extraction_svc = get_extraction_service()
            chunking_svc = get_chunking_service()

            service = DocumentService(
                document_repo=doc_repo,
                course_repo=course_repo,
                storage_client=storage_client,
                chunk_repo=chunk_repo,
                embedding_adapter=embedding_adapter,
                extraction_service=extraction_svc,
                chunking_service=chunking_svc,
            )

            processed_doc = await service.process_document_ingestion(
                document_id=doc_uuid,
                user_id=user_uuid,
                raise_on_error=True,
            )
            await session.commit()
            return processed_doc.status

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        final_status = loop.run_until_complete(_execute_ingestion())
        return {
            "document_id": document_id,
            "status": final_status,
        }
    except Exception:
        logger.exception("Task ingest_document_task failed for document %s", document_id)
        # Re-raise for Celery retry policy if transient
        raise
