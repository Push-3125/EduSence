"""Comprehensive unit and integration tests for Celery worker setup and Task Idempotency Guard.

Validates EduSense Invariant 8:
"Every worker task passes through the Idempotency Guard before execution."
"""

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.worker.celery_app import celery_app
from app.worker.idempotency import (
    DEFAULT_IDEMPOTENCY_PREFIX,
    IdempotencyGuard,
    IdempotencyStatus,
    idempotent_task,
)


class MockRedisClient:
    """In-memory mock Redis client supporting TTL and atomic SET NX EX operations."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expires_at: dict[str, float] = {}

    def _purge_expired(self, key: str) -> None:
        if key in self.expires_at and time.monotonic() > self.expires_at[key]:
            self.store.pop(key, None)
            self.expires_at.pop(key, None)

    def set(
        self,
        name: str,
        value: Any,
        ex: float | None = None,
        nx: bool = False,
    ) -> bool:
        self._purge_expired(name)
        if nx and name in self.store:
            return False

        self.store[name] = str(value)
        if ex is not None:
            self.expires_at[name] = time.monotonic() + ex
        else:
            self.expires_at.pop(name, None)
        return True

    def get(self, name: str) -> str | None:
        self._purge_expired(name)
        return self.store.get(name)

    def delete(self, *names: str) -> int:
        count = 0
        for name in names:
            if name in self.store:
                self.store.pop(name, None)
                self.expires_at.pop(name, None)
                count += 1
        return count

    def ttl(self, name: str) -> int:
        self._purge_expired(name)
        if name not in self.store:
            return -2
        if name not in self.expires_at:
            return -1
        remaining = int(self.expires_at[name] - time.monotonic())
        return max(remaining, 0)


class FailingRedisClient:
    """Mock Redis client simulating connection loss."""

    def set(self, *args: Any, **kwargs: Any) -> bool:
        raise RedisConnectionError("Simulated Redis connection failure")

    def get(self, *args: Any, **kwargs: Any) -> str | None:
        raise RedisConnectionError("Simulated Redis connection failure")

    def delete(self, *args: Any, **kwargs: Any) -> int:
        raise RedisConnectionError("Simulated Redis connection failure")


@pytest.fixture
def mock_redis() -> MockRedisClient:
    """Provide a clean mock Redis client."""
    return MockRedisClient()


@pytest.fixture
def guard(mock_redis: MockRedisClient) -> IdempotencyGuard:
    """Provide an IdempotencyGuard backed by mock Redis."""
    return IdempotencyGuard(redis_client=mock_redis)


# ---------------------------------------------------------------------------
# 1. Celery Application Configuration Tests
# ---------------------------------------------------------------------------


def test_celery_configuration_settings() -> None:
    """Verify Celery app initialization adheres to architecture invariants."""
    conf = celery_app.conf

    # Invariant configuration
    assert conf.timezone == "UTC"
    assert conf.enable_utc is True
    assert conf.task_serializer == "json"
    assert conf.result_serializer == "json"
    assert conf.accept_content == ["json"]
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_default_queue == "default"
    assert conf.task_track_started is True
    assert conf.broker_connection_retry_on_startup is True

    # Task routes verification
    routes = conf.task_routes
    assert isinstance(routes, dict)
    assert "app.worker.tasks.document.*" in routes
    assert routes["app.worker.tasks.document.*"]["queue"] == "documents"
    assert "app.worker.tasks.email.*" in routes
    assert routes["app.worker.tasks.email.*"]["queue"] == "emails"
    assert "app.worker.tasks.subscription.*" in routes
    assert routes["app.worker.tasks.subscription.*"]["queue"] == "subscriptions"


# ---------------------------------------------------------------------------
# 2. Idempotency Lock Lifecycle Tests (Acquire, Complete, Duplicate Prevention)
# ---------------------------------------------------------------------------


def test_first_execution_acquires_lock_and_marks_completed(
    guard: IdempotencyGuard,
    mock_redis: MockRedisClient,
) -> None:
    """Verify first run acquires IN_PROGRESS lock and transition to COMPLETED."""
    task_key = "doc_chunk_42"
    task_name = "document_processing"

    # 1. Initial acquire
    assert guard.acquire(key=task_key, task_name=task_name) is True

    # State in Redis is IN_PROGRESS
    full_key = f"{DEFAULT_IDEMPOTENCY_PREFIX}:{task_name}:{task_key}"
    assert mock_redis.get(full_key) == IdempotencyStatus.IN_PROGRESS.value
    assert guard.get_status(task_key, task_name=task_name) == IdempotencyStatus.IN_PROGRESS.value
    assert guard.is_done(task_key, task_name=task_name) is False

    # 2. Mark complete
    guard.complete(key=task_key, task_name=task_name)
    assert mock_redis.get(full_key) == IdempotencyStatus.COMPLETED.value
    assert guard.get_status(task_key, task_name=task_name) == IdempotencyStatus.COMPLETED.value
    assert guard.is_done(task_key, task_name=task_name) is True

    # 3. Subsequent acquire is skipped
    assert guard.acquire(key=task_key, task_name=task_name) is False


def test_duplicate_execution_skipped_when_completed(guard: IdempotencyGuard) -> None:
    """Verify tasks marked COMPLETED reject subsequent acquire attempts."""
    task_key = "email_reminder_99"
    guard.complete(task_key)

    assert guard.acquire(task_key) is False
    assert guard.is_done(task_key) is True


def test_concurrent_execution_blocked(guard: IdempotencyGuard) -> None:
    """Verify concurrent attempts on same key block the second execution."""
    task_key = "long_summary_12"

    # Worker 1 acquires lock
    worker_1 = guard.acquire(task_key)
    assert worker_1 is True

    # Worker 2 tries concurrently while IN_PROGRESS
    worker_2 = guard.acquire(task_key)
    assert worker_2 is False


def test_multithreaded_concurrency_only_one_acquires(guard: IdempotencyGuard) -> None:
    """Verify only 1 worker wins under multi-threaded concurrency competition."""
    task_key = "concurrent_race_test"

    def attempt_acquire(worker_id: int) -> bool:
        return guard.acquire(task_key)

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(attempt_acquire, range(10)))

    # Exactly one thread succeeds
    assert results.count(True) == 1
    assert results.count(False) == 9


# ---------------------------------------------------------------------------
# 3. Failed Execution & Retry Semantics
# ---------------------------------------------------------------------------


def test_failed_execution_allows_retry(guard: IdempotencyGuard) -> None:
    """Verify failed tasks set status to FAILED and allow subsequent retry acquire."""
    task_key = "sub_downgrade_user_7"

    # Worker acquires lock
    assert guard.acquire(task_key) is True

    # Task fails
    guard.fail(task_key, delete_lock=False)
    assert guard.get_status(task_key) == IdempotencyStatus.FAILED.value
    assert guard.is_done(task_key) is False

    # Retry attempt can re-acquire lock
    assert guard.acquire(task_key) is True
    assert guard.get_status(task_key) == IdempotencyStatus.IN_PROGRESS.value


def test_fail_with_delete_lock_allows_immediate_acquire(guard: IdempotencyGuard) -> None:
    """Verify fail with delete_lock=True removes key and allows immediate acquire."""
    task_key = "task_delete_lock"

    assert guard.acquire(task_key) is True
    guard.fail(task_key, delete_lock=True)

    assert guard.get_status(task_key) is None
    assert guard.acquire(task_key) is True


def test_release_deletes_lock(guard: IdempotencyGuard) -> None:
    """Verify explicit release removes the lock key."""
    task_key = "task_to_release"

    guard.acquire(task_key)
    guard.release(task_key)
    assert guard.get_status(task_key) is None
    assert guard.acquire(task_key) is True


def test_pending_task_transitions_to_in_progress(
    guard: IdempotencyGuard,
    mock_redis: MockRedisClient,
) -> None:
    """Verify tasks initialized as PENDING can be acquired by a worker."""
    task_key = "task_pre_enqueued"
    full_key = guard.resolve_key(task_key)
    mock_redis.set(full_key, IdempotencyStatus.PENDING.value)

    assert guard.get_status(task_key) == IdempotencyStatus.PENDING.value
    assert guard.acquire(task_key) is True
    assert guard.get_status(task_key) == IdempotencyStatus.IN_PROGRESS.value


# ---------------------------------------------------------------------------
# 4. Lock Expiration and TTL Tests
# ---------------------------------------------------------------------------


def test_lock_expiration_permits_reacquire(
    guard: IdempotencyGuard,
    mock_redis: MockRedisClient,
) -> None:
    """Verify lock expiration allows re-acquisition (worker crash safety)."""
    task_key = "crash_worker_key"

    # Acquire lock with 60-second TTL
    assert guard.acquire(task_key, ttl_seconds=60) is True

    full_key = guard.resolve_key(task_key)
    assert mock_redis.ttl(full_key) > 0

    # Simulate worker death and lock expiration (fast-forward timestamp)
    mock_redis.expires_at[full_key] = time.monotonic() - 1

    # New worker should now be able to acquire the expired task
    assert guard.acquire(task_key) is True


# ---------------------------------------------------------------------------
# 5. Redis Error Handling & Fail-Closed Behavior
# ---------------------------------------------------------------------------


def test_redis_failure_fail_closed() -> None:
    """Verify IdempotencyGuard blocks execution when Redis fails with fail_closed=True."""
    failing_redis = FailingRedisClient()
    guard = IdempotencyGuard(redis_client=failing_redis, fail_closed=True)

    assert guard.acquire("key_fail_closed") is False
    assert guard.get_status("key_fail_closed") is None


def test_redis_failure_fail_open() -> None:
    """Verify IdempotencyGuard permits execution when fail_closed=False."""
    failing_redis = FailingRedisClient()
    guard = IdempotencyGuard(redis_client=failing_redis, fail_closed=False)

    assert guard.acquire("key_fail_open") is True


def test_fail_and_complete_dont_raise_on_redis_error() -> None:
    """Verify complete, fail, and release gracefully catch Redis errors."""
    failing_redis = FailingRedisClient()
    guard = IdempotencyGuard(redis_client=failing_redis)

    # Should not raise exceptions
    guard.complete("key_err")
    guard.fail("key_err")
    guard.release("key_err")


# ---------------------------------------------------------------------------
# 6. @idempotent_task Decorator Tests (Sync, Async, Arguments, Errors)
# ---------------------------------------------------------------------------


def test_idempotent_task_decorator_sync(guard: IdempotencyGuard) -> None:
    """Verify decorator enforces idempotency on synchronous functions."""
    execution_count = 0

    @idempotent_task(key_param="task_id", guard=guard)
    def process_data(task_id: str, data: str) -> str:
        nonlocal execution_count
        execution_count += 1
        return f"processed:{data}"

    # First execution succeeds
    res1 = process_data("id-101", "first-run")
    assert res1 == "processed:first-run"
    assert execution_count == 1

    # Duplicate execution skipped
    res2 = process_data("id-101", "second-run")
    assert res2 is None
    assert execution_count == 1

    # Different key executes
    res3 = process_data("id-102", "third-run")
    assert res3 == "processed:third-run"
    assert execution_count == 2


@pytest.mark.asyncio
async def test_idempotent_task_decorator_async(guard: IdempotencyGuard) -> None:
    """Verify decorator enforces idempotency on asynchronous coroutine functions."""
    call_count = 0

    @idempotent_task(key_param="job_id", guard=guard)
    async def async_job(job_id: str) -> str:
        nonlocal call_count
        call_count += 1
        return f"job_result_{job_id}"

    # First execution
    res1 = await async_job(job_id="async-1")
    assert res1 == "job_result_async-1"
    assert call_count == 1

    # Second execution skipped
    res2 = await async_job(job_id="async-1")
    assert res2 is None
    assert call_count == 1


def test_idempotent_task_decorator_failure_and_retry(guard: IdempotencyGuard) -> None:
    """Verify failed tasks in decorator can be retried and succeed on second attempt."""
    attempt = 0

    @idempotent_task(key_param="item_id", guard=guard)
    def flaky_task(item_id: str) -> str:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise ValueError("Temporary failure")
        return "success"

    # 1. First attempt fails
    with pytest.raises(ValueError, match="Temporary failure"):
        flaky_task(item_id="item-500")

    assert guard.get_status("item-500", task_name="flaky_task") == IdempotencyStatus.FAILED.value

    # 2. Second attempt retries and succeeds
    res = flaky_task(item_id="item-500")
    assert res == "success"
    assert (
        guard.get_status("item-500", task_name="flaky_task") == IdempotencyStatus.COMPLETED.value
    )

    # 3. Third attempt is skipped
    assert flaky_task(item_id="item-500") is None


def test_idempotent_task_argument_extraction_variations(guard: IdempotencyGuard) -> None:
    """Verify key parameter extraction from positional, keyword, index, and callable."""

    # 1. Positional arg binding
    @idempotent_task(key_param="doc_id", guard=guard)
    def func_positional(doc_id: str, extra: int) -> str:
        return doc_id

    assert func_positional("pos-1", 10) == "pos-1"
    assert func_positional("pos-1", 20) is None

    # 2. Keyword arg
    @idempotent_task(key_param="key_arg", guard=guard)
    def func_keyword(*, key_arg: str) -> str:
        return key_arg

    assert func_keyword(key_arg="kw-1") == "kw-1"
    assert func_keyword(key_arg="kw-1") is None

    # 3. Numeric positional index
    @idempotent_task(key_param="0", guard=guard)
    def func_by_index(first: str, second: str) -> str:
        return first

    assert func_by_index("idx-1", "ignore") == "idx-1"
    assert func_by_index("idx-1", "other") is None

    # 4. Callable key builder
    @idempotent_task(key_param=lambda a, b: f"{a}:{b}", guard=guard)
    def func_callable(a: str, b: str) -> str:
        return f"{a}:{b}"

    assert func_callable("user1", "order2") == "user1:order2"
    assert func_callable("user1", "order2") is None


def test_idempotent_task_missing_key_raises_value_error(guard: IdempotencyGuard) -> None:
    """Verify ValueError is raised if key parameter cannot be resolved."""

    @idempotent_task(key_param="non_existent_key", guard=guard)
    def bad_task(foo: str) -> str:
        return foo

    with pytest.raises(ValueError, match="could not be resolved"):
        bad_task("test")


def test_idempotent_task_custom_on_duplicate(guard: IdempotencyGuard) -> None:
    """Verify custom on_duplicate return value when task is skipped."""

    @idempotent_task(
        key_param="task_id",
        guard=guard,
        on_duplicate={"status": "skipped", "reason": "idempotent"},
    )
    def custom_dup(task_id: str) -> dict[str, str]:
        return {"status": "executed", "task_id": task_id}

    res1 = custom_dup("dup-1")
    assert res1 == {"status": "executed", "task_id": "dup-1"}

    res2 = custom_dup("dup-1")
    assert res2 == {"status": "skipped", "reason": "idempotent"}


# ---------------------------------------------------------------------------
# 7. Architecture camelCase Aliases Tests
# ---------------------------------------------------------------------------


def test_architecture_camel_case_aliases(guard: IdempotencyGuard) -> None:
    """Verify architecture camelCase method aliases (isDone, markInProgress, markDone, markFailed)."""
    key = "alias_test_key"

    # markInProgress
    assert guard.markInProgress(key) is True
    assert guard.isDone(key) is False

    # markFailed
    guard.markFailed(key)
    assert guard.get_status(key) == IdempotencyStatus.FAILED.value

    # markDone
    guard.markDone(key)
    assert guard.isDone(key) is True
