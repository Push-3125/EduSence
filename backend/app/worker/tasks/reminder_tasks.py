"""Celery background worker and scheduler tasks for deadline and exam email reminders (US 2.19).

Adheres strictly to EduSense Invariants:
- Invariant 8: Every worker task passes through the Idempotency Guard before execution.
- Invariant 9: Celery Beat scheduler triggers tasks; database deduplication prevents duplicate notifications.
- Section 9 & 10: Mock SMTP in test mode; graceful failure on transport errors.
"""

from __future__ import annotations

import asyncio
import html
import logging
import uuid
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from app.core.database import async_session_factory
from app.repositories.assignment_repo import AssignmentRepository
from app.repositories.exam_repo import ExamRepository
from app.repositories.notification_repo import NotificationRepository
from app.services.email_service import get_email_service
from app.worker.celery_app import celery_app
from app.worker.idempotency import idempotent_task

logger = logging.getLogger("edusense.worker.reminders")

T = TypeVar("T")


def _run_async(coro_fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Execute an async coroutine from a synchronous Celery task worker safely."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro_fn())).result()
    return asyncio.run(coro_fn())


def _extract_assignment_reminder_key(*args: Any, **kwargs: Any) -> str:
    """Extract or generate task idempotency key for assignment deadline scan."""
    if kwargs.get("run_key"):
        return str(kwargs["run_key"])
    for arg in args:
        if isinstance(arg, str) and arg.startswith(("run_", "test_", "job_")):
            return arg
    return f"assignment_scan_{uuid.uuid4().hex}"


def _extract_exam_reminder_key(*args: Any, **kwargs: Any) -> str:
    """Extract or generate task idempotency key for exam reminder scan."""
    if kwargs.get("run_key"):
        return str(kwargs["run_key"])
    for arg in args:
        if isinstance(arg, str) and arg.startswith(("run_", "test_", "job_")):
            return arg
    return f"exam_scan_{uuid.uuid4().hex}"


@celery_app.task(
    name="app.worker.tasks.reminder_tasks.scan_assignment_deadline_reminders",
    max_retries=3,
    default_retry_delay=60,
)
@idempotent_task(
    task_name="scan_assignment_deadline_reminders",
    key_param=_extract_assignment_reminder_key,
)
def scan_assignment_deadline_reminders(
    hours_ahead: int = 24,
    run_key: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Scan pending assignments due within the next `hours_ahead` hours and send reminder emails.

    Args:
        hours_ahead: Lookahead window in hours (default 24).
        run_key: Optional custom idempotency key for task execution.
        **kwargs: Additional parameters.

    Returns:
        Dict summarizing execution results (status, scanned, sent, skipped).
    """
    async def _execute() -> dict[str, Any]:
        async with async_session_factory() as session:
            assignment_repo = AssignmentRepository(session)
            notif_repo = NotificationRepository(session)
            email_svc = get_email_service()

            now = datetime.now(UTC)
            max_due = now + timedelta(hours=hours_ahead)

            records = await assignment_repo.list_pending_due_between(
                start_dt=now,
                end_dt=max_due,
            )
            scanned_count = len(records)
            sent_count = 0
            skipped_count = 0

            for assignment, course, user_email in records:
                if not assignment.due_at:
                    skipped_count += 1
                    continue

                due_dt = (
                    assignment.due_at
                    if assignment.due_at.tzinfo
                    else assignment.due_at.replace(tzinfo=UTC)
                )
                dedup_key = f"assignment:{assignment.id}:{due_dt.date()}"

                # Deduplication check: only 1 reminder email per assignment per due date
                already_sent = await notif_repo.exists_by_dedup_key(
                    user_id=course.user_id,
                    channel="email",
                    deduplication_key=dedup_key,
                )
                if already_sent:
                    skipped_count += 1
                    continue

                due_str = due_dt.strftime("%Y-%m-%d %H:%M UTC")
                subject = f"Deadline Reminder: {assignment.title}"
                body_text = (
                    f"Hello,\n\n"
                    f"This is a reminder that your assignment '{assignment.title}' for course '{course.name}' "
                    f"is due on {due_str}.\n\n"
                    f"Please ensure your work is submitted on time.\n\n"
                    f"Best regards,\nEduSense Team"
                )
                body_html = (
                    f"<p>Hello,</p>"
                    f"<p>This is a reminder that your assignment <strong>{html.escape(assignment.title)}</strong> "
                    f"for course <strong>{html.escape(course.name)}</strong> is due on <strong>{due_str}</strong>.</p>"
                    f"<p>Please ensure your work is submitted on time.</p>"
                    f"<p>Best regards,<br>EduSense Team</p>"
                )

                email_sent = email_svc.send_email(
                    to_email=user_email,
                    subject=subject,
                    body_text=body_text,
                    body_html=body_html,
                )

                if email_sent:
                    await notif_repo.create(
                        user_id=course.user_id,
                        notification_type="deadline_reminder",
                        category="assignments",
                        title=f"Deadline Reminder: {assignment.title}",
                        content=f"Assignment '{assignment.title}' for {course.name} is due at {due_str}.",
                        channel="email",
                        deduplication_key=dedup_key,
                        target_url=f"/assignments/{assignment.id}",
                        status="sent",
                        sent_at=datetime.now(UTC),
                    )
                    sent_count += 1
                else:
                    logger.error(
                        "Failed to send deadline reminder email to %s for assignment %s",
                        user_email,
                        assignment.id,
                    )

            await session.commit()
            return {
                "status": "completed",
                "scanned": scanned_count,
                "sent": sent_count,
                "skipped": skipped_count,
            }

    return _run_async(_execute)


@celery_app.task(
    name="app.worker.tasks.reminder_tasks.scan_upcoming_exam_reminders",
    max_retries=3,
    default_retry_delay=60,
)
@idempotent_task(
    task_name="scan_upcoming_exam_reminders",
    key_param=_extract_exam_reminder_key,
)
def scan_upcoming_exam_reminders(
    days_ahead: int = 3,
    run_key: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Scan upcoming exams within `days_ahead` days and send reminder emails.

    Args:
        days_ahead: Lookahead window in days (default 3).
        run_key: Optional custom idempotency key for task execution.
        **kwargs: Additional parameters.

    Returns:
        Dict summarizing execution results (status, scanned, sent, skipped).
    """
    async def _execute() -> dict[str, Any]:
        async with async_session_factory() as session:
            exam_repo = ExamRepository(session)
            notif_repo = NotificationRepository(session)
            email_svc = get_email_service()

            now = datetime.now(UTC)
            max_exam = now + timedelta(days=days_ahead)

            records = await exam_repo.list_exams_between(
                start_dt=now,
                end_dt=max_exam,
            )
            scanned_count = len(records)
            sent_count = 0
            skipped_count = 0

            for exam, course, user_email in records:
                exam_dt = (
                    exam.exam_at
                    if exam.exam_at.tzinfo
                    else exam.exam_at.replace(tzinfo=UTC)
                )
                dedup_key = f"exam:{exam.id}:{exam_dt.date()}"

                # Deduplication check: only 1 reminder email per exam per scheduled date
                already_sent = await notif_repo.exists_by_dedup_key(
                    user_id=course.user_id,
                    channel="email",
                    deduplication_key=dedup_key,
                )
                if already_sent:
                    skipped_count += 1
                    continue

                exam_str = exam_dt.strftime("%Y-%m-%d %H:%M UTC")
                subject = f"Exam Reminder: {exam.title}"
                location_text = exam.location or "N/A"
                duration_text = f"{exam.duration_minutes} minutes" if exam.duration_minutes else "N/A"

                body_text = (
                    f"Hello,\n\n"
                    f"This is a reminder that your exam '{exam.title}' for course '{course.name}' "
                    f"is scheduled on {exam_str}.\n"
                    f"Location: {location_text}\n"
                    f"Duration: {duration_text}\n\n"
                    f"Good luck with your preparation!\n\n"
                    f"Best regards,\nEduSense Team"
                )
                body_html = (
                    f"<p>Hello,</p>"
                    f"<p>This is a reminder that your exam <strong>{html.escape(exam.title)}</strong> "
                    f"for course <strong>{html.escape(course.name)}</strong> is scheduled on <strong>{exam_str}</strong>.</p>"
                    f"<p><strong>Location:</strong> {html.escape(location_text)}<br>"
                    f"<strong>Duration:</strong> {html.escape(duration_text)}</p>"
                    f"<p>Good luck with your preparation!</p>"
                    f"<p>Best regards,<br>EduSense Team</p>"
                )

                email_sent = email_svc.send_email(
                    to_email=user_email,
                    subject=subject,
                    body_text=body_text,
                    body_html=body_html,
                )

                if email_sent:
                    await notif_repo.create(
                        user_id=course.user_id,
                        notification_type="exam_reminder",
                        category="exams",
                        title=f"Exam Reminder: {exam.title}",
                        content=f"Exam '{exam.title}' for {course.name} is scheduled on {exam_str}.",
                        channel="email",
                        deduplication_key=dedup_key,
                        target_url=f"/exams/{exam.id}",
                        status="sent",
                        sent_at=datetime.now(UTC),
                    )
                    sent_count += 1
                else:
                    logger.error(
                        "Failed to send exam reminder email to %s for exam %s",
                        user_email,
                        exam.id,
                    )

            await session.commit()
            return {
                "status": "completed",
                "scanned": scanned_count,
                "sent": sent_count,
                "skipped": skipped_count,
            }

    return _run_async(_execute)


__all__ = [
    "scan_assignment_deadline_reminders",
    "scan_upcoming_exam_reminders",
]
