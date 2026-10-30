"""Celery worker task for scanning expired subscriptions and auto-downgrading to Free plan (TASK-M6-03).

Adheres strictly to EduSense Invariants:
- Invariant 8: Every worker task passes through the Idempotency Guard before execution.
- Invariant 9: Periodic tasks run via Celery Beat; DB consistency maintained atomically.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, TypeVar

from app.core.database import async_session_factory
from app.repositories.notification_repo import NotificationRepository
from app.repositories.payment_repo import PaymentRepository
from app.worker.celery_app import celery_app
from app.worker.idempotency import idempotent_task

logger = logging.getLogger("edusense.worker.downgrade")

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


def _extract_downgrade_scan_key(*args: Any, **kwargs: Any) -> str:
    """Extract or generate task idempotency key for subscription downgrade scan."""
    if kwargs.get("run_key"):
        return str(kwargs["run_key"])
    for arg in args:
        if isinstance(arg, str) and (
            arg.startswith(("run_", "test_", "job_", "scan_")) or len(arg) > 0
        ):
            return arg
    return f"downgrade_scan_{uuid.uuid4().hex}"


@celery_app.task(
    name="app.worker.tasks.downgrade_task.scan_and_downgrade_expired_subscriptions",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
@idempotent_task(
    task_type="subscription_downgrade_scan",
    key_extractor=_extract_downgrade_scan_key,
)
def scan_and_downgrade_expired_subscriptions(
    self: Any = None,
    limit: int = 100,
    run_key: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Scan active subscriptions that have expired and auto-downgrade them to Free plan.

    Args:
        self: Bound Celery task instance.
        limit: Max batch size to scan and downgrade (default 100).
        run_key: Optional idempotency key override.
        **kwargs: Additional arguments.

    Returns:
        Dict summarizing execution results (scanned, downgraded, processed_ids).
    """
    # Normalize if invoked directly without Celery's bound self
    if isinstance(self, int):
        limit = self
        self = None

    async def _execute() -> dict[str, Any]:
        async with async_session_factory() as session:
            payment_repo = PaymentRepository(session)
            notif_repo = NotificationRepository(session)

            expired_subs = await payment_repo.get_expired_active_subscriptions(limit=limit)
            scanned_count = len(expired_subs)
            downgraded_count = 0
            processed_ids: list[str] = []

            for sub in expired_subs:
                sub_id = sub.id
                user_id = sub.user_id

                success = await payment_repo.batch_downgrade_subscription(
                    subscription_id=sub_id,
                    user_id=user_id,
                )
                if success:
                    downgraded_count += 1
                    processed_ids.append(str(sub_id))

                    # Create in-app system notification via NotificationRepository
                    await notif_repo.create(
                        user_id=user_id,
                        notification_type="system",
                        category="progress",
                        title="Gói Pro đã hết hạn",
                        content="Gói Pro của bạn đã hết hạn. Tài khoản đã được chuyển về gói Miễn phí (Free).",
                        channel="in_app",
                        deduplication_key=f"sub_expired:{sub_id}",
                        status="sent",
                        sent_at=datetime.now(UTC),
                    )
                    await session.commit()
                    logger.info(
                        "Auto-downgraded expired subscription %s for user %s to Free plan",
                        sub_id,
                        user_id,
                    )
                else:
                    logger.warning(
                        "Skipped downgrade for subscription %s (already expired or cancelled)",
                        sub_id,
                    )

            return {
                "scanned": scanned_count,
                "downgraded": downgraded_count,
                "processed_ids": processed_ids,
            }

    try:
        return _run_async(_execute)
    except Exception as exc:
        logger.exception("scan_and_downgrade_expired_subscriptions failed")
        if hasattr(self, "retry"):
            raise self.retry(exc=exc)
        raise
