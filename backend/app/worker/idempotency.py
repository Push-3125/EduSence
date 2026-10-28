"""Redis-backed Idempotency Guard for EduSense background workers and Celery tasks.

Adheres strictly to EduSense Invariant 8:
"Every worker task passes through the Idempotency Guard before execution."
"""

import functools
import inspect
import logging
from collections.abc import Callable
from enum import Enum
from typing import Any

from redis import Redis, from_url
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_IDEMPOTENCY_PREFIX: str = "edusense:idempotency"


class IdempotencyStatus(str, Enum):
    """Task idempotency execution statuses."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class IdempotencyError(Exception):
    """Base exception for task idempotency violations or Redis storage failures."""


def _normalize_status(val: Any) -> str | None:
    """Normalize raw Redis status value to standard uppercase string."""
    if val is None:
        return None
    if isinstance(val, bytes):
        val = val.decode("utf-8")
    val_str = str(val).strip().upper()
    if val_str in ("DONE", "COMPLETED"):
        return IdempotencyStatus.COMPLETED.value
    if val_str == "IN_PROGRESS":
        return IdempotencyStatus.IN_PROGRESS.value
    if val_str == "FAILED":
        return IdempotencyStatus.FAILED.value
    if val_str == "PENDING":
        return IdempotencyStatus.PENDING.value
    return val_str


class IdempotencyGuard:
    """Redis-backed Idempotency Guard preventing duplicate task execution.

    Key pattern: `edusense:idempotency:{task_name}:{task_key}`.
    Tracks state transitions: PENDING -> IN_PROGRESS -> COMPLETED | FAILED.
    - COMPLETED tasks are skipped permanently (within retention TTL).
    - IN_PROGRESS tasks prevent concurrent execution.
    - FAILED tasks release the lock or allow immediate retry on next attempt.
    """

    def __init__(
        self,
        redis_client: Redis | Any | None = None,
        default_task_name: str = "default",
        prefix: str = DEFAULT_IDEMPOTENCY_PREFIX,
        fail_closed: bool = True,
    ) -> None:
        self._redis = redis_client
        self.default_task_name = default_task_name
        self.prefix = prefix
        self.fail_closed = fail_closed

    @property
    def redis(self) -> Any:
        """Return synchronous Redis connection singleton."""
        if self._redis is None:
            self._redis = from_url(settings.redis_url, decode_responses=True)
        return self._redis

    def format_key(self, task_name: str | None, task_key: str) -> str:
        """Format full Redis key with namespace prefix and task identifier."""
        name = task_name or self.default_task_name
        return f"{self.prefix}:{name}:{task_key}"

    def resolve_key(self, key: str, task_name: str | None = None) -> str:
        """Resolve full Redis key string, preserving already-prefixed keys."""
        if key.startswith(f"{self.prefix}:"):
            return key
        return self.format_key(task_name, key)

    def acquire(
        self,
        key: str,
        task_name: str | None = None,
        ttl_seconds: int = 3600,
    ) -> bool:
        """Atomically attempt to acquire the execution lock for a task.

        Returns True if execution is permitted:
        - Key does not exist (lock acquired atomically with SET NX EX).
        - Key was marked PENDING or FAILED (retry / pick-up allowed).
        Returns False if:
        - Key is COMPLETED (task already executed successfully).
        - Key is IN_PROGRESS (concurrent execution running).
        """
        full_key = self.resolve_key(key, task_name)
        try:
            # 1. Attempt atomic SET NX EX lock acquisition
            acquired = self.redis.set(
                full_key,
                IdempotencyStatus.IN_PROGRESS.value,
                nx=True,
                ex=ttl_seconds,
            )
            if acquired:
                return True

            # 2. Key exists: inspect current status
            raw_val = self.redis.get(full_key)
            if raw_val is None:
                # Key expired immediately after SET NX
                retry_acquired = self.redis.set(
                    full_key,
                    IdempotencyStatus.IN_PROGRESS.value,
                    nx=True,
                    ex=ttl_seconds,
                )
                return bool(retry_acquired)

            status = _normalize_status(raw_val)

            if status == IdempotencyStatus.COMPLETED.value:
                logger.info("Task key '%s' is already COMPLETED. Skipping execution.", full_key)
                return False

            if status == IdempotencyStatus.IN_PROGRESS.value:
                logger.warning(
                    "Task key '%s' is currently IN_PROGRESS. Blocking duplicate execution.",
                    full_key,
                )
                return False

            if status in (IdempotencyStatus.PENDING.value, IdempotencyStatus.FAILED.value):
                # Transition pending task or allow retry for failed task
                self.redis.set(full_key, IdempotencyStatus.IN_PROGRESS.value, ex=ttl_seconds)
                return True

            return False
        except (RedisError, ConnectionError, OSError, TimeoutError) as exc:
            logger.error(
                "Redis connection failure in IdempotencyGuard.acquire for key '%s': %s",
                full_key,
                exc,
            )
            if self.fail_closed:
                return False
            logger.warning(
                "IdempotencyGuard fail_closed=False; allowing execution despite Redis failure"
            )
            return True

    def complete(
        self,
        key: str,
        task_name: str | None = None,
        ttl_seconds: int = 86400,
    ) -> None:
        """Mark task as COMPLETED with TTL (default 24h) to skip duplicate executions."""
        full_key = self.resolve_key(key, task_name)
        try:
            self.redis.set(full_key, IdempotencyStatus.COMPLETED.value, ex=ttl_seconds)
        except (RedisError, ConnectionError, OSError, TimeoutError) as exc:
            logger.warning("Redis error marking task '%s' as completed: %s", full_key, exc)

    def fail(
        self,
        key: str,
        task_name: str | None = None,
        ttl_seconds: int = 300,
        delete_lock: bool = False,
    ) -> None:
        """Mark task as FAILED with TTL so retries can re-acquire after backoff, or release lock."""
        full_key = self.resolve_key(key, task_name)
        try:
            if delete_lock:
                self.redis.delete(full_key)
            else:
                self.redis.set(full_key, IdempotencyStatus.FAILED.value, ex=ttl_seconds)
        except (RedisError, ConnectionError, OSError, TimeoutError) as exc:
            logger.warning("Redis error marking task '%s' as failed: %s", full_key, exc)

    def release(self, key: str, task_name: str | None = None) -> None:
        """Explicitly delete the idempotency lock key."""
        full_key = self.resolve_key(key, task_name)
        try:
            self.redis.delete(full_key)
        except (RedisError, ConnectionError, OSError, TimeoutError) as exc:
            logger.warning("Redis error releasing lock '%s': %s", full_key, exc)

    def get_status(self, key: str, task_name: str | None = None) -> str | None:
        """Retrieve current normalized idempotency status."""
        full_key = self.resolve_key(key, task_name)
        try:
            raw_val = self.redis.get(full_key)
            return _normalize_status(raw_val)
        except (RedisError, ConnectionError, OSError, TimeoutError) as exc:
            logger.warning("Redis error retrieving status for '%s': %s", full_key, exc)
            return None

    # Architecture aliases (EduSense Source of Truth & Architecture Spec)
    def is_done(self, key: str, task_name: str | None = None) -> bool:
        """Check if task has already completed."""
        return self.get_status(key, task_name) == IdempotencyStatus.COMPLETED.value

    def isDone(self, key: str, task_name: str | None = None) -> bool:
        """Architecture camelCase alias for is_done."""
        return self.is_done(key, task_name)

    def mark_in_progress(
        self,
        key: str,
        task_name: str | None = None,
        ttl_seconds: int = 3600,
    ) -> bool:
        """Set task state to in_progress."""
        return self.acquire(key, task_name=task_name, ttl_seconds=ttl_seconds)

    def markInProgress(
        self,
        key: str,
        task_name: str | None = None,
        ttl_seconds: int = 3600,
    ) -> bool:
        """Architecture camelCase alias for mark_in_progress."""
        return self.mark_in_progress(key, task_name=task_name, ttl_seconds=ttl_seconds)

    def mark_done(
        self,
        key: str,
        task_name: str | None = None,
        ttl_seconds: int = 86400,
    ) -> None:
        """Set task state to done/completed."""
        self.complete(key, task_name=task_name, ttl_seconds=ttl_seconds)

    def markDone(
        self,
        key: str,
        task_name: str | None = None,
        ttl_seconds: int = 86400,
    ) -> None:
        """Architecture camelCase alias for mark_done."""
        self.mark_done(key, task_name=task_name, ttl_seconds=ttl_seconds)

    def mark_failed(
        self,
        key: str,
        task_name: str | None = None,
        ttl_seconds: int = 300,
        delete_lock: bool = False,
    ) -> None:
        """Set task state to failed."""
        self.fail(key, task_name=task_name, ttl_seconds=ttl_seconds, delete_lock=delete_lock)

    def markFailed(
        self,
        key: str,
        task_name: str | None = None,
        ttl_seconds: int = 300,
        delete_lock: bool = False,
    ) -> None:
        """Architecture camelCase alias for mark_failed."""
        self.mark_failed(key, task_name=task_name, ttl_seconds=ttl_seconds, delete_lock=delete_lock)


# Global singleton instance holder
_global_guard: IdempotencyGuard | None = None


def get_idempotency_guard() -> IdempotencyGuard:
    """Return the global IdempotencyGuard instance."""
    global _global_guard
    if _global_guard is None:
        _global_guard = IdempotencyGuard()
    return _global_guard


def set_idempotency_guard(guard: IdempotencyGuard | None) -> None:
    """Override or reset the global IdempotencyGuard instance (useful for tests)."""
    global _global_guard
    _global_guard = guard


def _extract_task_key(
    func: Callable[..., Any],
    key_param: str | Callable[..., str],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    """Extract idempotency key from function arguments."""
    if callable(key_param):
        return str(key_param(*args, **kwargs))

    # 1. Direct keyword lookup
    if key_param in kwargs:
        return str(kwargs[key_param])

    # 2. Inspect signature to bind arguments
    try:
        sig = inspect.signature(func)
        bound = sig.bind_partial(*args, **kwargs)
        if key_param in bound.arguments:
            return str(bound.arguments[key_param])
    except (TypeError, ValueError):
        pass

    # 3. Numeric positional index
    if key_param.isdigit():
        idx = int(key_param)
        if idx < len(args):
            return str(args[idx])

    raise ValueError(
        f"Idempotency key parameter '{key_param}' could not be resolved from arguments "
        f"for function '{func.__name__}'"
    )


def idempotent_task(
    key_param: str | Callable[..., str],
    ttl_seconds: int = 3600,
    task_name: str | None = None,
    guard: IdempotencyGuard | None = None,
    on_duplicate: Any = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to enforce task idempotency using IdempotencyGuard.

    Args:
        key_param: Name of argument or callable to extract unique task key.
        ttl_seconds: Lock duration for in-progress execution (default 1 hour).
        task_name: Custom task namespace (defaults to wrapped function __name__).
        guard: Custom IdempotencyGuard instance (defaults to global singleton).
        on_duplicate: Value to return when task execution is skipped (default None).
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        task_identifier = task_name or getattr(func, "__name__", "task")

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                active_guard = guard or get_idempotency_guard()
                task_key = _extract_task_key(func, key_param, args, kwargs)

                if not active_guard.acquire(
                    key=task_key,
                    task_name=task_identifier,
                    ttl_seconds=ttl_seconds,
                ):
                    logger.info(
                        "Idempotent task '%s' with key '%s' skipped (already completed or in progress).",
                        task_identifier,
                        task_key,
                    )
                    return on_duplicate

                try:
                    result = await func(*args, **kwargs)
                    active_guard.complete(key=task_key, task_name=task_identifier)
                    return result
                except Exception:
                    active_guard.fail(key=task_key, task_name=task_identifier)
                    raise

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            active_guard = guard or get_idempotency_guard()
            task_key = _extract_task_key(func, key_param, args, kwargs)

            if not active_guard.acquire(
                key=task_key,
                task_name=task_identifier,
                ttl_seconds=ttl_seconds,
            ):
                logger.info(
                    "Idempotent task '%s' with key '%s' skipped (already completed or in progress).",
                    task_identifier,
                    task_key,
                )
                return on_duplicate

            try:
                result = func(*args, **kwargs)
                active_guard.complete(key=task_key, task_name=task_identifier)
                return result
            except Exception:
                active_guard.fail(key=task_key, task_name=task_identifier)
                raise

        return sync_wrapper

    return decorator
