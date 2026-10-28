"""Background Celery tasks package for EduSense."""

from app.worker.tasks.document_tasks import ingest_document_task
from app.worker.tasks.reminder_tasks import (
    scan_assignment_deadline_reminders,
    scan_upcoming_exam_reminders,
)
from app.worker.tasks.summary_tasks import generate_summary_task

__all__ = [
    "generate_summary_task",
    "ingest_document_task",
    "scan_assignment_deadline_reminders",
    "scan_upcoming_exam_reminders",
]
