"""Comprehensive tests for Celery Beat Schedulers, Reminder Tasks, and Email Service (TASK-M5-08, US 2.19).

Validates:
- Invariant 8: Every worker task passes through the Idempotency Guard. Avoid duplicate sends on retry or recurring cron runs.
- Section 9 & 10: Mock SMTP in tests; NEVER connect to real external SMTP or send real emails.
- Deduplication:
  * Assignment deadline reminders deduplicate via PostgreSQL unique constraint/key `assignment:{id}:{date}`.
  * Exam reminders deduplicate via PostgreSQL unique constraint/key `exam:{id}:{date}`.
- Celery Beat schedule configuration for hourly assignment scanning and daily exam scanning.
"""

from __future__ import annotations

import smtplib
import sys
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from celery.schedules import crontab  # type: ignore

# Ensure backend root is on sys.path for direct execution
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings
from app.repositories.assignment_repo import Assignment, AssignmentRepository
from app.repositories.course_repo import Course
from app.repositories.exam_repo import Exam, ExamRepository
from app.repositories.notification_repo import Notification, NotificationRepository
from app.repositories.user_repo import User
from app.services.email_service import (
    EmailService,
    get_email_service,
    set_email_service,
)
from app.worker.celery_app import celery_app
from app.worker.idempotency import (
    IdempotencyGuard,
    IdempotencyStatus,
    get_idempotency_guard,
    set_idempotency_guard,
)
from app.worker.tasks.reminder_tasks import (
    scan_assignment_deadline_reminders,
    scan_upcoming_exam_reminders,
)

# ---------------------------------------------------------------------------
# Test Doubles & Mocks
# ---------------------------------------------------------------------------


class MockRedisClient:
    """In-memory mock Redis client supporting TTL and atomic SET NX operations."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expires_at: dict[str, float] = {}

    def set(
        self,
        name: str,
        value: Any,
        ex: float | None = None,
        nx: bool = False,
    ) -> bool:
        if nx and name in self.store:
            return False
        self.store[name] = str(value)
        return True

    def get(self, name: str) -> str | None:
        return self.store.get(name)

    def delete(self, *names: str) -> int:
        count = 0
        for name in names:
            if name in self.store:
                del self.store[name]
                count += 1
        return count


class FakeNotificationRepository(NotificationRepository):
    """In-memory notification repository for scheduler tests."""

    def __init__(self) -> None:
        self.notifications: list[Notification] = []
        self.dedup_keys: set[tuple[uuid.UUID, str, str]] = set()

    async def create(
        self,
        user_id: uuid.UUID,
        notification_type: str,
        category: str,
        title: str,
        content: str,
        channel: str = "in_app",
        deduplication_key: str | None = None,
        target_url: str | None = None,
        scheduled_at: datetime | None = None,
        sent_at: datetime | None = None,
        status: str = "pending",
    ) -> Notification:
        notif = Notification(
            id=uuid.uuid4(),
            user_id=user_id,
            notification_type=notification_type,
            category=category,
            title=title,
            content=content,
            channel=channel,
            deduplication_key=deduplication_key,
            target_url=target_url,
            scheduled_at=scheduled_at,
            sent_at=sent_at,
            status=status,
            is_read=False,
            read_at=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.notifications.append(notif)
        if deduplication_key:
            self.dedup_keys.add((user_id, channel, deduplication_key))
        return notif

    async def exists_by_dedup_key(
        self,
        user_id: uuid.UUID,
        channel: str,
        deduplication_key: str,
    ) -> bool:
        return (user_id, channel, deduplication_key) in self.dedup_keys


class FakeAssignmentRepository(AssignmentRepository):
    """In-memory assignment repository for scheduler tests."""

    def __init__(self) -> None:
        self.records: list[tuple[Assignment, Course, str]] = []

    def add_assignment(
        self,
        assignment: Assignment,
        course: Course,
        user_email: str,
    ) -> None:
        self.records.append((assignment, course, user_email))

    async def list_pending_due_between(
        self,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[tuple[Assignment, Course, str]]:
        start = start_dt if start_dt.tzinfo else start_dt.replace(tzinfo=UTC)
        end = end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=UTC)

        matched: list[tuple[Assignment, Course, str]] = []
        for assignment, course, email in self.records:
            if assignment.status == "completed":
                continue
            if not assignment.due_at:
                continue
            due = assignment.due_at if assignment.due_at.tzinfo else assignment.due_at.replace(tzinfo=UTC)
            if start <= due <= end:
                matched.append((assignment, course, email))
        return matched


class FakeExamRepository(ExamRepository):
    """In-memory exam repository for scheduler tests."""

    def __init__(self) -> None:
        self.records: list[tuple[Exam, Course, str]] = []

    def add_exam(
        self,
        exam: Exam,
        course: Course,
        user_email: str,
    ) -> None:
        self.records.append((exam, course, user_email))

    async def list_exams_between(
        self,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[tuple[Exam, Course, str]]:
        start = start_dt if start_dt.tzinfo else start_dt.replace(tzinfo=UTC)
        end = end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=UTC)

        matched: list[tuple[Exam, Course, str]] = []
        for exam, course, email in self.records:
            exam_time = exam.exam_at if exam.exam_at.tzinfo else exam.exam_at.replace(tzinfo=UTC)
            if start <= exam_time <= end:
                matched.append((exam, course, email))
        return matched


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_globals() -> Any:
    """Reset global email service and idempotency guard before and after each test."""
    original_app_env = settings.app_env
    test_email_svc = EmailService(test_mode=True)
    set_email_service(test_email_svc)

    mock_redis = MockRedisClient()
    guard = IdempotencyGuard(redis_client=mock_redis)
    set_idempotency_guard(guard)

    yield

    set_email_service(None)
    set_idempotency_guard(None)
    settings.app_env = original_app_env


@pytest.fixture
def fake_notif_repo() -> FakeNotificationRepository:
    return FakeNotificationRepository()


@pytest.fixture
def fake_assignment_repo() -> FakeAssignmentRepository:
    return FakeAssignmentRepository()


@pytest.fixture
def fake_exam_repo() -> FakeExamRepository:
    return FakeExamRepository()


def _build_test_user_and_course() -> tuple[User, Course]:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email="student@university.edu.vn",
        password_hash="mock_hash",
        is_active=True,
    )
    course = Course(
        id=uuid.uuid4(),
        user_id=user_id,
        course_code="CS101",
        name="Introduction to Computer Science",
    )
    course.user = user
    return user, course


# ---------------------------------------------------------------------------
# 1. EmailService Unit Tests
# ---------------------------------------------------------------------------


def test_email_service_test_mode_records_email() -> None:
    """Verify EmailService records emails in memory without network I/O in test mode."""
    service = EmailService(test_mode=True)
    success = service.send_email(
        to_email="student@example.com",
        subject="Test Notification",
        body_text="Plain text body",
        body_html="<p>HTML body</p>",
    )

    assert success is True
    assert len(service.sent_emails) == 1
    record = service.sent_emails[0]
    assert record["to_email"] == "student@example.com"
    assert record["subject"] == "Test Notification"
    assert record["body_text"] == "Plain text body"
    assert record["body_html"] == "<p>HTML body</p>"
    assert isinstance(record["sent_at"], datetime)


def test_email_service_invalid_recipient_returns_false() -> None:
    """Verify invalid email address is rejected cleanly."""
    service = EmailService(test_mode=True)
    assert service.send_email("", "Subject", "Body") is False
    assert service.send_email("invalid-address-without-at", "Subject", "Body") is False
    assert len(service.sent_emails) == 0


def test_email_service_smtp_error_handled_gracefully() -> None:
    """Verify live SMTP errors are caught and logged, returning False."""
    service = EmailService(test_mode=False)

    with patch("smtplib.SMTP") as mock_smtp:
        mock_smtp.side_effect = smtplib.SMTPConnectError(421, b"Connection refused")
        success = service.send_email(
            to_email="student@example.com",
            subject="Test Subject",
            body_text="Test Body",
        )
        assert success is False


def test_email_service_clear_buffer() -> None:
    """Verify buffer clearing functionality."""
    service = EmailService(test_mode=True)
    service.send_email("user@example.com", "Subj", "Body")
    assert len(service.sent_emails) == 1
    service.clear_sent_emails()
    assert len(service.sent_emails) == 0


# ---------------------------------------------------------------------------
# 2. Assignment Deadline Scanner Tests
# ---------------------------------------------------------------------------


def test_scan_assignment_deadlines_due_in_12_hours(
    fake_notif_repo: FakeNotificationRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    """Assignment due in 12 hours -> email sent, notification recorded."""
    _user, course = _build_test_user_and_course()
    now = datetime.now(UTC)

    assignment = Assignment(
        id=uuid.uuid4(),
        course_id=course.id,
        title="Operating Systems Lab 3",
        status="pending",
        due_at=now + timedelta(hours=12),
    )
    fake_assignment_repo.add_assignment(assignment, course, "student@university.edu.vn")

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        session = AsyncMock()
        yield session

    with patch("app.worker.tasks.reminder_tasks.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.reminder_tasks.AssignmentRepository", return_value=fake_assignment_repo), \
         patch("app.worker.tasks.reminder_tasks.NotificationRepository", return_value=fake_notif_repo):

        result = scan_assignment_deadline_reminders(hours_ahead=24)

    assert result["status"] == "completed"
    assert result["scanned"] == 1
    assert result["sent"] == 1
    assert result["skipped"] == 0

    # Verify notification record created
    assert len(fake_notif_repo.notifications) == 1
    notif = fake_notif_repo.notifications[0]
    assert notif.user_id == course.user_id
    assert notif.category == "assignments"
    assert notif.channel == "email"
    assert notif.status == "sent"
    assert "Operating Systems Lab 3" in notif.title
    due_date_str = (now + timedelta(hours=12)).date()
    assert notif.deduplication_key == f"assignment:{assignment.id}:{due_date_str}"

    # Verify email sent
    email_svc = get_email_service()
    assert len(email_svc.sent_emails) == 1
    email = email_svc.sent_emails[0]
    assert email["to_email"] == "student@university.edu.vn"
    assert "Operating Systems Lab 3" in email["subject"]


def test_scan_assignment_deadlines_already_completed(
    fake_notif_repo: FakeNotificationRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    """Assignment already completed -> no email sent."""
    _user, course = _build_test_user_and_course()
    now = datetime.now(UTC)

    assignment = Assignment(
        id=uuid.uuid4(),
        course_id=course.id,
        title="Calculus Homework 4",
        status="completed",  # Completed!
        due_at=now + timedelta(hours=6),
    )
    fake_assignment_repo.add_assignment(assignment, course, "student@university.edu.vn")

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        session = AsyncMock()
        yield session

    with patch("app.worker.tasks.reminder_tasks.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.reminder_tasks.AssignmentRepository", return_value=fake_assignment_repo), \
         patch("app.worker.tasks.reminder_tasks.NotificationRepository", return_value=fake_notif_repo):

        result = scan_assignment_deadline_reminders(hours_ahead=24)

    assert result["status"] == "completed"
    assert result["scanned"] == 0
    assert result["sent"] == 0
    assert len(fake_notif_repo.notifications) == 0
    assert len(get_email_service().sent_emails) == 0


def test_scan_assignment_deadlines_due_in_5_days(
    fake_notif_repo: FakeNotificationRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    """Assignment due in 5 days (beyond 24-hour lookahead) -> no email sent."""
    _user, course = _build_test_user_and_course()
    now = datetime.now(UTC)

    assignment = Assignment(
        id=uuid.uuid4(),
        course_id=course.id,
        title="Final Project Milestone",
        status="pending",
        due_at=now + timedelta(days=5),  # 5 days in future
    )
    fake_assignment_repo.add_assignment(assignment, course, "student@university.edu.vn")

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        session = AsyncMock()
        yield session

    with patch("app.worker.tasks.reminder_tasks.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.reminder_tasks.AssignmentRepository", return_value=fake_assignment_repo), \
         patch("app.worker.tasks.reminder_tasks.NotificationRepository", return_value=fake_notif_repo):

        result = scan_assignment_deadline_reminders(hours_ahead=24)

    assert result["status"] == "completed"
    assert result["scanned"] == 0
    assert result["sent"] == 0
    assert len(get_email_service().sent_emails) == 0


def test_scan_assignment_deadlines_deduplication_prevents_second_send(
    fake_notif_repo: FakeNotificationRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    """Second run for the same assignment does NOT send duplicate email."""
    _user, course = _build_test_user_and_course()
    now = datetime.now(UTC)

    assignment = Assignment(
        id=uuid.uuid4(),
        course_id=course.id,
        title="Database Schema Design",
        status="pending",
        due_at=now + timedelta(hours=8),
    )
    fake_assignment_repo.add_assignment(assignment, course, "student@university.edu.vn")

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        session = AsyncMock()
        yield session

    with patch("app.worker.tasks.reminder_tasks.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.reminder_tasks.AssignmentRepository", return_value=fake_assignment_repo), \
         patch("app.worker.tasks.reminder_tasks.NotificationRepository", return_value=fake_notif_repo):

        # First run: should send email
        run1 = scan_assignment_deadline_reminders(hours_ahead=24)
        assert run1["sent"] == 1
        assert run1["skipped"] == 0

        # Second run: should detect deduplication key and skip sending email
        run2 = scan_assignment_deadline_reminders(hours_ahead=24)
        assert run2["sent"] == 0
        assert run2["skipped"] == 1

    email_svc = get_email_service()
    assert len(email_svc.sent_emails) == 1  # Exactly 1 email sent across both runs
    assert len(fake_notif_repo.notifications) == 1


# ---------------------------------------------------------------------------
# 3. Upcoming Exam Scanner Tests
# ---------------------------------------------------------------------------


def test_scan_upcoming_exams_in_2_days(
    fake_notif_repo: FakeNotificationRepository,
    fake_exam_repo: FakeExamRepository,
) -> None:
    """Exam scheduled in 2 days -> email sent, notification recorded."""
    _user, course = _build_test_user_and_course()
    now = datetime.now(UTC)

    exam = Exam(
        id=uuid.uuid4(),
        course_id=course.id,
        title="Midterm Examination",
        exam_at=now + timedelta(days=2),
        duration_minutes=90,
        location="Hall A-101",
    )
    fake_exam_repo.add_exam(exam, course, "student@university.edu.vn")

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        session = AsyncMock()
        yield session

    with patch("app.worker.tasks.reminder_tasks.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.reminder_tasks.ExamRepository", return_value=fake_exam_repo), \
         patch("app.worker.tasks.reminder_tasks.NotificationRepository", return_value=fake_notif_repo):

        result = scan_upcoming_exam_reminders(days_ahead=3)

    assert result["status"] == "completed"
    assert result["scanned"] == 1
    assert result["sent"] == 1
    assert result["skipped"] == 0

    # Verify notification record created
    assert len(fake_notif_repo.notifications) == 1
    notif = fake_notif_repo.notifications[0]
    assert notif.user_id == course.user_id
    assert notif.category == "exams"
    assert notif.channel == "email"
    assert notif.status == "sent"
    assert "Midterm Examination" in notif.title
    exam_date_str = (now + timedelta(days=2)).date()
    assert notif.deduplication_key == f"exam:{exam.id}:{exam_date_str}"

    # Verify email sent
    email_svc = get_email_service()
    assert len(email_svc.sent_emails) == 1
    email = email_svc.sent_emails[0]
    assert email["to_email"] == "student@university.edu.vn"
    assert "Midterm Examination" in email["subject"]
    assert "Hall A-101" in email["body_text"]
    assert "90 minutes" in email["body_text"]


def test_scan_upcoming_exams_in_14_days(
    fake_notif_repo: FakeNotificationRepository,
    fake_exam_repo: FakeExamRepository,
) -> None:
    """Exam scheduled in 14 days (beyond 3-day window) -> no email sent."""
    _user, course = _build_test_user_and_course()
    now = datetime.now(UTC)

    exam = Exam(
        id=uuid.uuid4(),
        course_id=course.id,
        title="Final Examination",
        exam_at=now + timedelta(days=14),
        duration_minutes=120,
    )
    fake_exam_repo.add_exam(exam, course, "student@university.edu.vn")

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        session = AsyncMock()
        yield session

    with patch("app.worker.tasks.reminder_tasks.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.reminder_tasks.ExamRepository", return_value=fake_exam_repo), \
         patch("app.worker.tasks.reminder_tasks.NotificationRepository", return_value=fake_notif_repo):

        result = scan_upcoming_exam_reminders(days_ahead=3)

    assert result["status"] == "completed"
    assert result["scanned"] == 0
    assert result["sent"] == 0
    assert len(get_email_service().sent_emails) == 0


def test_scan_upcoming_exams_deduplication_prevents_second_send(
    fake_notif_repo: FakeNotificationRepository,
    fake_exam_repo: FakeExamRepository,
) -> None:
    """Second run for the same exam does NOT send duplicate email."""
    _user, course = _build_test_user_and_course()
    now = datetime.now(UTC)

    exam = Exam(
        id=uuid.uuid4(),
        course_id=course.id,
        title="Computer Architecture Quiz",
        exam_at=now + timedelta(days=1),
        duration_minutes=45,
    )
    fake_exam_repo.add_exam(exam, course, "student@university.edu.vn")

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        session = AsyncMock()
        yield session

    with patch("app.worker.tasks.reminder_tasks.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.reminder_tasks.ExamRepository", return_value=fake_exam_repo), \
         patch("app.worker.tasks.reminder_tasks.NotificationRepository", return_value=fake_notif_repo):

        # First run: sends email
        run1 = scan_upcoming_exam_reminders(days_ahead=3)
        assert run1["sent"] == 1
        assert run1["skipped"] == 0

        # Second run: deduplication prevents duplicate send
        run2 = scan_upcoming_exam_reminders(days_ahead=3)
        assert run2["sent"] == 0
        assert run2["skipped"] == 1

    email_svc = get_email_service()
    assert len(email_svc.sent_emails) == 1  # Only 1 email sent in total
    assert len(fake_notif_repo.notifications) == 1


# ---------------------------------------------------------------------------
# 4. Idempotency Guard & Celery Beat Schedule Tests
# ---------------------------------------------------------------------------


def test_idempotency_guard_prevents_duplicate_task_invocation(
    fake_notif_repo: FakeNotificationRepository,
    fake_assignment_repo: FakeAssignmentRepository,
) -> None:
    """Verify that Idempotency Guard blocks identical task execution with same run_key."""
    _user, course = _build_test_user_and_course()
    now = datetime.now(UTC)

    assignment = Assignment(
        id=uuid.uuid4(),
        course_id=course.id,
        title="Physics Homework 2",
        status="pending",
        due_at=now + timedelta(hours=5),
    )
    fake_assignment_repo.add_assignment(assignment, course, "student@university.edu.vn")

    @asynccontextmanager
    async def mock_async_session() -> AsyncGenerator[AsyncMock, None]:
        session = AsyncMock()
        yield session

    task_key = "run_assignment_scan_1001"

    with patch("app.worker.tasks.reminder_tasks.async_session_factory", side_effect=mock_async_session), \
         patch("app.worker.tasks.reminder_tasks.AssignmentRepository", return_value=fake_assignment_repo), \
         patch("app.worker.tasks.reminder_tasks.NotificationRepository", return_value=fake_notif_repo):

        # First execution with task_key succeeds
        result1 = scan_assignment_deadline_reminders(hours_ahead=24, run_key=task_key)
        assert result1 is not None
        assert result1["status"] == "completed"

        # Check guard status is COMPLETED
        guard = get_idempotency_guard()
        status = guard.get_status(key=task_key, task_name="scan_assignment_deadline_reminders")
        assert status == IdempotencyStatus.COMPLETED.value

        # Second execution with identical task_key is intercepted by @idempotent_task
        result2 = scan_assignment_deadline_reminders(hours_ahead=24, run_key=task_key)
        assert result2 is None  # Skipped by @idempotent_task (on_duplicate default)


def test_celery_beat_schedule_configuration() -> None:
    """Verify Celery Beat schedule contains hourly assignment and daily exam schedules."""
    beat_schedule = celery_app.conf.beat_schedule
    assert beat_schedule is not None

    # 1. Assignment deadlines hourly task
    assert "scan-assignment-deadlines-hourly" in beat_schedule
    hourly_config = beat_schedule["scan-assignment-deadlines-hourly"]
    assert hourly_config["task"] == "app.worker.tasks.reminder_tasks.scan_assignment_deadline_reminders"
    assert hourly_config["kwargs"] == {"hours_ahead": 24}
    assert isinstance(hourly_config["schedule"], crontab)

    # 2. Exam reminders daily task
    assert "scan-exam-reminders-daily" in beat_schedule
    daily_config = beat_schedule["scan-exam-reminders-daily"]
    assert daily_config["task"] == "app.worker.tasks.reminder_tasks.scan_upcoming_exam_reminders"
    assert daily_config["kwargs"] == {"days_ahead": 3}
    assert isinstance(daily_config["schedule"], crontab)

    # 3. Task routes configuration
    task_routes = celery_app.conf.task_routes
    assert "app.worker.tasks.reminder_tasks.*" in task_routes
    assert task_routes["app.worker.tasks.reminder_tasks.*"]["queue"] == "emails"


# ---------------------------------------------------------------------------
# 5. Database Repository Logic Unit Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notification_repo_create_and_exists_by_dedup() -> None:
    """Verify NotificationRepository database statements."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    repo = NotificationRepository(mock_session)

    user_id = uuid.uuid4()
    notif = await repo.create(
        user_id=user_id,
        notification_type="deadline_reminder",
        category="assignments",
        title="Reminder",
        content="Due tomorrow",
        channel="email",
        deduplication_key="assignment:123:2026-10-29",
        status="sent",
    )
    assert notif.user_id == user_id
    assert notif.channel == "email"
    mock_session.add.assert_called_once()
    mock_session.flush.assert_called_once()

    # exists_by_dedup_key query
    mock_result = MagicMock()
    mock_result.scalar.return_value = 1
    mock_session.execute.return_value = mock_result

    exists = await repo.exists_by_dedup_key(user_id, "email", "assignment:123:2026-10-29")
    assert exists is True
    mock_session.execute.assert_called_once()
