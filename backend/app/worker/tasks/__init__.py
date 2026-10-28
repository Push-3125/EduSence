"""Background Celery tasks package for EduSense."""

from app.worker.tasks.document_tasks import ingest_document_task
from app.worker.tasks.summary_tasks import generate_summary_task

__all__ = [
    "generate_summary_task",
    "ingest_document_task",
]
