"""Comprehensive tests for EduSense AI Quota Reservation & Redis Fail-Closed Manager.

Tests adhere strictly to:
- US 2.21: Student quota management and tracking.
- Invariant 7 (DECISION-CONFIRMED-05): Check and reserve quota before calling Gemini;
  if Redis is down, fail closed. Refund on failure.
- DECISION-PROP-04: Free plan = 10 requests/day, Pro plan = 150 requests/day, warning at 80% usage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.services.quota_service import (
    DEFAULT_FREE_DAILY_QUOTA,
    DEFAULT_PRO_DAILY_QUOTA,
    KEY_TTL_SECONDS,
    AIQuotaService,
    QuotaExceededError,
    QuotaServiceUnavailableError,
    get_quota_service,
)


class MockAsyncRedisClient:
    """In-memory asynchronous Redis mock double supporting Lua scripts."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl_map: dict[str, int] = {}

    async def get(self, name: str) -> str | None:
        return self.store.get(name)

    async def set(
        self,
        name: str,
        value: Any,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        if nx and name in self.store:
            return False
        self.store[name] = str(value)
        if ex is not None:
            self.ttl_map[name] = ex
        return True

    async def delete(self, *names: str) -> int:
        count = 0
        for name in names:
            if name in self.store:
                del self.store[name]
                self.ttl_map.pop(name, None)
                count += 1
        return count

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        key = str(keys_and_args[0])
        # Differentiate between RESERVE and REFUND scripts
        if "INCR" in script:
            limit = int(keys_and_args[1])
            ttl = int(keys_and_args[2])
            current = self.store.get(key)
            if current is not None and int(current) >= limit:
                return -1
            new_val = (int(current) if current is not None else 0) + 1
            self.store[key] = str(new_val)
            if key not in self.ttl_map:
                self.ttl_map[key] = ttl
            return new_val
        elif "DECR" in script:
            current = self.store.get(key)
            if current is None or int(current) <= 0:
                return 0
            new_val = int(current) - 1
            self.store[key] = str(new_val)
            return new_val
        return 0


class FailingAsyncRedisClient:
    """Mock Redis client simulating connection or timeout errors."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or RedisConnectionError("Simulated Redis connection failure")

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise self.exc

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        raise self.exc

    async def delete(self, *args: Any, **kwargs: Any) -> Any:
        raise self.exc

    async def eval(self, *args: Any, **kwargs: Any) -> Any:
        raise self.exc


@pytest.fixture
def mock_redis() -> MockAsyncRedisClient:
    return MockAsyncRedisClient()


@pytest.fixture
def quota_service(mock_redis: MockAsyncRedisClient) -> AIQuotaService:
    return AIQuotaService(redis_client=mock_redis)


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.UUID("550e8400-e29b-41d4-a716-446655440000")


# ===========================================================================
# 1. Plan Limit & Mapping Tests
# ===========================================================================


def test_plan_limit_mapping(quota_service: AIQuotaService) -> None:
    """Verify plan limits per DECISION-PROP-04."""
    assert quota_service.get_limit_for_plan("free") == DEFAULT_FREE_DAILY_QUOTA  # 10
    assert quota_service.get_limit_for_plan("FREE") == DEFAULT_FREE_DAILY_QUOTA
    assert quota_service.get_limit_for_plan("pro") == DEFAULT_PRO_DAILY_QUOTA  # 150
    assert quota_service.get_limit_for_plan("PRO") == DEFAULT_PRO_DAILY_QUOTA
    assert quota_service.get_limit_for_plan("unknown") == DEFAULT_FREE_DAILY_QUOTA
    assert quota_service.get_limit_for_plan(None) == DEFAULT_FREE_DAILY_QUOTA


def test_quota_key_formatting(quota_service: AIQuotaService, user_id: uuid.UUID) -> None:
    """Verify Redis key formatting matches quota:ai:{user_id}:{date_utc}."""
    target = date(2026, 10, 28)
    key = quota_service.get_quota_key(user_id, target_date=target)
    assert key == f"quota:ai:{user_id}:2026-10-28"

    # String user_id coercion
    key_str = quota_service.get_quota_key(str(user_id), target_date=target)
    assert key_str == f"quota:ai:{user_id}:2026-10-28"


# ===========================================================================
# 2. Check Quota Tests (Read-Only)
# ===========================================================================


@pytest.mark.asyncio
async def test_check_quota_zero_usage(
    quota_service: AIQuotaService, user_id: uuid.UUID
) -> None:
    """Check quota for free user with 0 usage: remaining = 10, is_warning = False, can_proceed = True."""
    status = await quota_service.check_quota(user_id=user_id, plan_code="free")
    assert status.user_id == user_id
    assert status.plan_code == "free"
    assert status.limit == 10
    assert status.used == 0
    assert status.remaining == 10
    assert status.is_warning is False
    assert status.can_proceed is True
    assert status.reset_at.tzinfo == UTC
    assert status.reset_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_check_quota_at_warning_threshold(
    quota_service: AIQuotaService,
    mock_redis: MockAsyncRedisClient,
    user_id: uuid.UUID,
) -> None:
    """Check quota at 80% usage (8/10): is_warning = True, can_proceed = True."""
    key = quota_service.get_quota_key(user_id)
    mock_redis.store[key] = "8"

    status = await quota_service.check_quota(user_id=user_id, plan_code="free")
    assert status.limit == 10
    assert status.used == 8
    assert status.remaining == 2
    assert status.is_warning is True
    assert status.can_proceed is True


@pytest.mark.asyncio
async def test_check_quota_at_100_percent_usage(
    quota_service: AIQuotaService,
    mock_redis: MockAsyncRedisClient,
    user_id: uuid.UUID,
) -> None:
    """Check quota at 100% usage (10/10): remaining = 0, is_warning = True, can_proceed = False."""
    key = quota_service.get_quota_key(user_id)
    mock_redis.store[key] = "10"

    status = await quota_service.check_quota(user_id=user_id, plan_code="free")
    assert status.limit == 10
    assert status.used == 10
    assert status.remaining == 0
    assert status.is_warning is True
    assert status.can_proceed is False


@pytest.mark.asyncio
async def test_check_quota_pro_plan(
    quota_service: AIQuotaService,
    mock_redis: MockAsyncRedisClient,
    user_id: uuid.UUID,
) -> None:
    """Verify Pro plan quota limit (150 requests/day)."""
    key = quota_service.get_quota_key(user_id)
    mock_redis.store[key] = "119"  # < 80% of 150 (120)

    status = await quota_service.check_quota(user_id=user_id, plan_code="pro")
    assert status.limit == 150
    assert status.used == 119
    assert status.remaining == 31
    assert status.is_warning is False
    assert status.can_proceed is True

    # Now at 120 (exactly 80%)
    mock_redis.store[key] = "120"
    status_warn = await quota_service.check_quota(user_id=user_id, plan_code="pro")
    assert status_warn.is_warning is True
    assert status_warn.can_proceed is True


# ===========================================================================
# 3. Atomic Reservation Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_reserve_quota_success_increments_counter_and_sets_ttl(
    quota_service: AIQuotaService,
    mock_redis: MockAsyncRedisClient,
    user_id: uuid.UUID,
) -> None:
    """Successful atomic reservation increments counter and sets TTL."""
    key = quota_service.get_quota_key(user_id)

    # First reservation
    status1 = await quota_service.reserve_quota(user_id=user_id, plan_code="free")
    assert status1.used == 1
    assert status1.remaining == 9
    assert status1.can_proceed is True
    assert status1.is_warning is False
    assert mock_redis.store[key] == "1"
    assert mock_redis.ttl_map[key] == KEY_TTL_SECONDS

    # Second reservation
    status2 = await quota_service.reserve_quota(user_id=user_id, plan_code="free")
    assert status2.used == 2
    assert status2.remaining == 8
    assert mock_redis.store[key] == "2"


@pytest.mark.asyncio
async def test_reserve_quota_reaches_warning_threshold(
    quota_service: AIQuotaService,
    mock_redis: MockAsyncRedisClient,
    user_id: uuid.UUID,
) -> None:
    """Reservation reaching 80% usage flags is_warning = True."""
    key = quota_service.get_quota_key(user_id)
    mock_redis.store[key] = "7"

    status = await quota_service.reserve_quota(user_id=user_id, plan_code="free")
    assert status.used == 8
    assert status.remaining == 2
    assert status.is_warning is True
    assert status.can_proceed is True


@pytest.mark.asyncio
async def test_reserve_quota_exceeding_limit_raises_error(
    quota_service: AIQuotaService,
    mock_redis: MockAsyncRedisClient,
    user_id: uuid.UUID,
) -> None:
    """Atomic reservation exceeding limit raises QuotaExceededError."""
    key = quota_service.get_quota_key(user_id)
    mock_redis.store[key] = "10"  # Free plan limit reached

    with pytest.raises(QuotaExceededError) as exc_info:
        await quota_service.reserve_quota(user_id=user_id, plan_code="free")

    assert "Daily AI quota of 10 requests reached" in str(exc_info.value)
    # Counter must not be incremented past limit
    assert mock_redis.store[key] == "10"


# ===========================================================================
# 4. Fail-Closed Resilience Tests (Invariant 7)
# ===========================================================================


@pytest.mark.asyncio
async def test_reserve_quota_fails_closed_on_connection_error(
    user_id: uuid.UUID,
) -> None:
    """When Redis throws ConnectionError, reserve_quota raises QuotaServiceUnavailableError (FAIL-CLOSED!)."""
    failing_client = FailingAsyncRedisClient(RedisConnectionError("Connection refused"))
    service = AIQuotaService(redis_client=failing_client)

    with pytest.raises(QuotaServiceUnavailableError) as exc_info:
        await service.reserve_quota(user_id=user_id, plan_code="free")

    assert "AI quota service temporarily unavailable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_reserve_quota_fails_closed_on_timeout_error(
    user_id: uuid.UUID,
) -> None:
    """When Redis throws TimeoutError, reserve_quota raises QuotaServiceUnavailableError (FAIL-CLOSED!)."""
    failing_client = FailingAsyncRedisClient(RedisTimeoutError("Redis timed out after 3.0s"))
    service = AIQuotaService(redis_client=failing_client)

    with pytest.raises(QuotaServiceUnavailableError) as exc_info:
        await service.reserve_quota(user_id=user_id, plan_code="free")

    assert "AI quota service temporarily unavailable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_check_quota_fails_closed_on_redis_error(
    user_id: uuid.UUID,
) -> None:
    """When Redis is down during check_quota, fail closed by raising QuotaServiceUnavailableError."""
    failing_client = FailingAsyncRedisClient(RedisConnectionError("Unreachable"))
    service = AIQuotaService(redis_client=failing_client)

    with pytest.raises(QuotaServiceUnavailableError):
        await service.check_quota(user_id=user_id, plan_code="free")


# ===========================================================================
# 5. Atomic Refund Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_refund_quota_decrements_counter(
    quota_service: AIQuotaService,
    mock_redis: MockAsyncRedisClient,
    user_id: uuid.UUID,
) -> None:
    """Refund quota decrements counter down to 0 minimum."""
    key = quota_service.get_quota_key(user_id)
    mock_redis.store[key] = "5"

    await quota_service.refund_quota(user_id=user_id)
    assert mock_redis.store[key] == "4"

    # Decrement again
    await quota_service.refund_quota(user_id=user_id)
    assert mock_redis.store[key] == "3"


@pytest.mark.asyncio
async def test_refund_quota_floor_zero(
    quota_service: AIQuotaService,
    mock_redis: MockAsyncRedisClient,
    user_id: uuid.UUID,
) -> None:
    """Refund quota when counter is 0 or non-existent does not decrement into negative numbers."""
    key = quota_service.get_quota_key(user_id)
    mock_redis.store[key] = "0"

    await quota_service.refund_quota(user_id=user_id)
    assert mock_redis.store[key] == "0"

    # Test non-existent key
    mock_redis.store.clear()
    await quota_service.refund_quota(user_id=user_id)
    # Floor remains at 0, no negative key created
    assert mock_redis.store.get(key, "0") == "0"


@pytest.mark.asyncio
async def test_refund_quota_redis_failure_does_not_crash(
    user_id: uuid.UUID,
) -> None:
    """If Redis fails during refund, logs warning but does not crash."""
    failing_client = FailingAsyncRedisClient(RedisConnectionError("Redis down"))
    service = AIQuotaService(redis_client=failing_client)

    # Should not raise exception
    await service.refund_quota(user_id=user_id)


# ===========================================================================
# 6. Reset Quota Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_reset_quota(
    quota_service: AIQuotaService,
    mock_redis: MockAsyncRedisClient,
    user_id: uuid.UUID,
) -> None:
    """Reset quota deletes the key from Redis."""
    key = quota_service.get_quota_key(user_id)
    mock_redis.store[key] = "9"

    await quota_service.reset_quota(user_id=user_id)
    assert key not in mock_redis.store


# ===========================================================================
# 7. Async Context Manager Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_context_manager_success_reserves_and_does_not_refund(
    quota_service: AIQuotaService,
    mock_redis: MockAsyncRedisClient,
    user_id: uuid.UUID,
) -> None:
    """On success: quota is reserved, context block executes, quota is NOT refunded."""
    key = quota_service.get_quota_key(user_id)
    executed = False

    async with quota_service.reserve(user_id=user_id, plan_code="free") as status:
        executed = True
        assert status.used == 1
        assert status.can_proceed is True
        assert mock_redis.store[key] == "1"

    assert executed is True
    # Quota was NOT refunded because context block finished successfully
    assert mock_redis.store[key] == "1"


@pytest.mark.asyncio
async def test_context_manager_failure_inside_block_auto_refunds(
    quota_service: AIQuotaService,
    mock_redis: MockAsyncRedisClient,
    user_id: uuid.UUID,
) -> None:
    """On failure inside block: quota reserved then automatically refunded and re-raises."""
    key = quota_service.get_quota_key(user_id)
    mock_redis.store[key] = "4"

    class GeminiApiError(Exception):
        pass

    with pytest.raises(GeminiApiError):
        async with quota_service.reserve(user_id=user_id, plan_code="free") as status:
            assert status.used == 5
            assert mock_redis.store[key] == "5"
            raise GeminiApiError("Gemini 503 Service Unavailable")

    # Quota was automatically refunded back from 5 to 4!
    assert mock_redis.store[key] == "4"


@pytest.mark.asyncio
async def test_context_manager_quota_exceeded_blocks_execution(
    quota_service: AIQuotaService,
    mock_redis: MockAsyncRedisClient,
    user_id: uuid.UUID,
) -> None:
    """When quota already exceeded: context manager does not execute block and raises QuotaExceededError."""
    key = quota_service.get_quota_key(user_id)
    mock_redis.store[key] = "10"
    block_executed = False

    with pytest.raises(QuotaExceededError):
        async with quota_service.reserve(user_id=user_id, plan_code="free"):
            block_executed = True

    assert block_executed is False
    assert mock_redis.store[key] == "10"


@pytest.mark.asyncio
async def test_context_manager_redis_down_fails_closed(
    user_id: uuid.UUID,
) -> None:
    """When Redis is down: context manager raises QuotaServiceUnavailableError and blocks call."""
    failing_client = FailingAsyncRedisClient(RedisConnectionError("Redis down"))
    service = AIQuotaService(redis_client=failing_client)
    block_executed = False

    with pytest.raises(QuotaServiceUnavailableError):
        async with service.reserve(user_id=user_id, plan_code="free"):
            block_executed = True

    assert block_executed is False


# ===========================================================================
# 8. Dependency Provider Tests
# ===========================================================================


def test_get_quota_service_singleton() -> None:
    """get_quota_service() returns a valid AIQuotaService singleton."""
    service1 = get_quota_service()
    service2 = get_quota_service()
    assert isinstance(service1, AIQuotaService)
    assert service1 is service2


# ===========================================================================
# 9. Explicit AsyncMock Unit Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_reserve_quota_with_async_mock(user_id: uuid.UUID) -> None:
    """Explicitly verify AIQuotaService interacts with Redis via AsyncMock."""
    mock_redis_client = AsyncMock()
    mock_redis_client.eval.return_value = 1

    service = AIQuotaService(redis_client=mock_redis_client)
    status = await service.reserve_quota(user_id=user_id, plan_code="free")

    assert status.used == 1
    assert status.remaining == 9
    assert status.can_proceed is True
    mock_redis_client.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_reserve_quota_exceeded_with_async_mock(user_id: uuid.UUID) -> None:
    """Verify AsyncMock returning -1 triggers QuotaExceededError."""
    mock_redis_client = AsyncMock()
    mock_redis_client.eval.return_value = -1

    service = AIQuotaService(redis_client=mock_redis_client)
    with pytest.raises(QuotaExceededError):
        await service.reserve_quota(user_id=user_id, plan_code="free")


@pytest.mark.asyncio
async def test_refund_quota_with_async_mock(user_id: uuid.UUID) -> None:
    """Verify refund_quota awaits eval on Redis client."""
    mock_redis_client = AsyncMock()
    mock_redis_client.eval.return_value = 2

    service = AIQuotaService(redis_client=mock_redis_client)
    await service.refund_quota(user_id=user_id)
    mock_redis_client.eval.assert_awaited_once()

