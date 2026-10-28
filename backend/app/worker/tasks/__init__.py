"""Background Celery tasks package for EduSense."""

from app.worker.tasks.document_tasks import ingest_document_task

__all__ = [
    "ingest_document_task",
]
