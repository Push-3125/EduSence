"""Atomic Quota Reservation and Redis Fail-Closed Manager for EduSense AI features.

Adheres strictly to:
- US 2.21: Student quota management and tracking.
- EduSense Invariant 7 (DECISION-CONFIRMED-05): Check and reserve quota before calling Gemini
  (atomic Redis counter); if Redis is down, fail closed. Refund on failure.
- DECISION-PROP-04: Free plan = 10 requests/day, Pro plan = 150 requests/day, warning at 80% usage.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from redis.asyncio import from_url as redis_from_url
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger("edusense.services.quota")

# Constants per DECISION-PROP-04 and architecture specification
DEFAULT_FREE_DAILY_QUOTA: int = 10
DEFAULT_PRO_DAILY_QUOTA: int = 150
WARNING_THRESHOLD_PERCENT: float = 80.0
KEY_TTL_SECONDS: int = 86400 * 2  # 48 hours retention for daily counters

# Lua script for atomic reservation:
# Checks if current usage >= limit; if so, returns -1 (quota exceeded).
# Otherwise, increments counter, ensures TTL is set if newly created, and returns new count.
RESERVE_LUA_SCRIPT: str = """
local current = redis.call('GET', KEYS[1])
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])

if current and tonumber(current) >= limit then
    return -1
end

local new_val = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ttl)
end
return new_val
"""

# Lua script for atomic refund with floor 0:
# Decrements counter if positive; does not create negative counters.
REFUND_LUA_SCRIPT: str = """
local current = redis.call('GET', KEYS[1])
if not current then
    return 0
end
local val = tonumber(current)
if val <= 0 then
    return 0
end
return redis.call('DECR', KEYS[1])
"""


# ---------------------------------------------------------------------------
# Domain Exceptions
# ---------------------------------------------------------------------------


class QuotaError(Exception):
    """Base exception for quota-related domain errors."""


class QuotaExceededError(QuotaError):
    """Raised when user has reached or exceeded their daily quota limit."""


class QuotaServiceUnavailableError(QuotaError):
    """Raised when quota check/reservation fails due to Redis unavailability (fail-closed)."""


# ---------------------------------------------------------------------------
# Domain Data Transfer Objects
# ---------------------------------------------------------------------------


@dataclass
class QuotaStatus:
    """Represents current AI request quota usage and limits for a user."""

    user_id: uuid.UUID
    plan_code: str
    limit: int
    used: int
    remaining: int
    reset_at: datetime
    is_warning: bool
    can_proceed: bool


# ---------------------------------------------------------------------------
# AI Quota Service
# ---------------------------------------------------------------------------


class AIQuotaService:
    """Manages atomic reservation and refunding of AI generation quotas backed by Redis.

    Enforces EduSense Invariant 7:
    - Atomically increments counter before LLM execution via Lua script.
    - If Redis is unreachable, fails closed (raises QuotaServiceUnavailableError).
    - Context manager ensures automatic refund on downstream generation failures.
    """

    def __init__(
        self,
        redis_client: Redis | Any | None = None,
        free_limit: int = DEFAULT_FREE_DAILY_QUOTA,
        pro_limit: int = DEFAULT_PRO_DAILY_QUOTA,
        warning_threshold_percent: float = WARNING_THRESHOLD_PERCENT,
        key_ttl_seconds: int = KEY_TTL_SECONDS,
    ) -> None:
        """Initialize AIQuotaService.

        Args:
            redis_client: Optional pre-configured Redis client (used for tests/dependency injection).
            free_limit: Daily quota limit for free plan users (default: 10).
            pro_limit: Daily quota limit for pro plan users (default: 150).
            warning_threshold_percent: Usage percent triggering warning flag (default: 80.0%).
            key_ttl_seconds: TTL in seconds for quota keys (default: 48 hours).
        """
        self._redis = redis_client
        self._free_limit = free_limit
        self._pro_limit = pro_limit
        self.warning_threshold_percent = warning_threshold_percent
        self.key_ttl_seconds = key_ttl_seconds

    @property
    def redis(self) -> Any:
        """Return asynchronous Redis client instance."""
        if self._redis is None:
            self._redis = redis_from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
        return self._redis

    @staticmethod
    def _coerce_uuid(user_id: uuid.UUID | str) -> uuid.UUID:
        """Ensure user_id is a valid UUID instance."""
        if isinstance(user_id, uuid.UUID):
            return user_id
        return uuid.UUID(str(user_id))

    def get_limit_for_plan(self, plan_code: str | None = "free") -> int:
        """Return daily request limit for the specified plan code.

        Maps:
            - 'free' -> DEFAULT_FREE_DAILY_QUOTA (10)
            - 'pro'  -> DEFAULT_PRO_DAILY_QUOTA (150)
            - Unknown/None -> defaults to free plan limit (case-insensitive).
        """
        if not plan_code:
            return self._free_limit
        normalized = plan_code.strip().lower()
        if normalized == "pro":
            return self._pro_limit
        return self._free_limit

    def update_limits(
        self,
        free_limit: int | None = None,
        pro_limit: int | None = None,
        warning_threshold_percent: float | None = None,
    ) -> None:
        """Update in-memory plan request ceilings and warning threshold."""
        if free_limit is not None:
            self._free_limit = free_limit
        if pro_limit is not None:
            self._pro_limit = pro_limit
        if warning_threshold_percent is not None:
            self.warning_threshold_percent = warning_threshold_percent

    def get_quota_key(
        self, user_id: uuid.UUID | str, target_date: date | None = None
    ) -> str:
        """Generate namespaced Redis key for user quota on a given UTC date.

        Format: `quota:ai:{user_id}:{date_utc}` (e.g. `quota:ai:550e8400...:2026-10-28`).
        """
        uid = self._coerce_uuid(user_id)
        if target_date is None:
            target_date = datetime.now(UTC).date()
        return f"quota:ai:{uid}:{target_date.strftime('%Y-%m-%d')}"

    @staticmethod
    def _get_next_reset_time(now: datetime | None = None) -> datetime:
        """Calculate next midnight UTC when daily quota resets."""
        if now is None:
            now = datetime.now(UTC)
        tomorrow = now.date() + timedelta(days=1)
        return datetime(
            year=tomorrow.year,
            month=tomorrow.month,
            day=tomorrow.day,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
            tzinfo=UTC,
        )

    async def check_quota(
        self,
        user_id: uuid.UUID | str,
        plan_code: str = "free",
        target_date: date | None = None,
    ) -> QuotaStatus:
        """Read current quota usage and limits without modifying state.

        Args:
            user_id: Student user UUID.
            plan_code: User subscription plan ('free' or 'pro').
            target_date: Target UTC date (defaults to current UTC date).

        Returns:
            QuotaStatus snapshot.

        Raises:
            QuotaServiceUnavailableError: If Redis is unreachable (fail-closed).
        """
        uid = self._coerce_uuid(user_id)
        limit = self.get_limit_for_plan(plan_code)
        key = self.get_quota_key(uid, target_date=target_date)

        try:
            raw_val = await self.redis.get(key)
        except (RedisError, ConnectionError, TimeoutError, OSError) as exc:
            logger.error("Redis error checking quota for user %s: %s", uid, exc)
            raise QuotaServiceUnavailableError(
                f"AI quota service temporarily unavailable: {exc}"
            ) from exc

        used = int(raw_val) if raw_val is not None else 0
        remaining = max(0, limit - used)
        warning_threshold = limit * (self.warning_threshold_percent / 100.0)
        is_warning = used >= warning_threshold
        can_proceed = used < limit
        reset_at = self._get_next_reset_time()

        return QuotaStatus(
            user_id=uid,
            plan_code=plan_code.lower().strip() if plan_code else "free",
            limit=limit,
            used=used,
            remaining=remaining,
            reset_at=reset_at,
            is_warning=is_warning,
            can_proceed=can_proceed,
        )

    async def reserve_quota(
        self,
        user_id: uuid.UUID | str,
        plan_code: str = "free",
        target_date: date | None = None,
    ) -> QuotaStatus:
        """Atomically reserve 1 request from user's daily quota.

        Executes atomic Redis Lua script:
        - If current usage >= limit: returns -1 and raises QuotaExceededError.
        - Otherwise increments counter, sets TTL, and returns new count.
        - If Redis fails: raises QuotaServiceUnavailableError (FAIL-CLOSED!).

        Args:
            user_id: Student user UUID.
            plan_code: User subscription tier ('free' or 'pro').
            target_date: Target UTC date (defaults to current UTC date).

        Returns:
            QuotaStatus with updated usage and can_proceed=True.

        Raises:
            QuotaExceededError: If daily limit has been reached.
            QuotaServiceUnavailableError: If Redis is unavailable (fail-closed).
        """
        uid = self._coerce_uuid(user_id)
        limit = self.get_limit_for_plan(plan_code)
        key = self.get_quota_key(uid, target_date=target_date)

        try:
            result = await self.redis.eval(
                RESERVE_LUA_SCRIPT,
                1,
                key,
                str(limit),
                str(self.key_ttl_seconds),
            )
        except (RedisError, ConnectionError, TimeoutError, OSError) as exc:
            logger.error("Redis error during AI quota reservation for user %s: %s", uid, exc)
            raise QuotaServiceUnavailableError(
                f"AI quota service temporarily unavailable: {exc}"
            ) from exc

        used = int(result)
        if used == -1:
            logger.info("AI quota exceeded for user %s (limit %d)", uid, limit)
            raise QuotaExceededError(
                f"Daily AI quota of {limit} requests reached for user {uid} ({plan_code} plan)."
            )

        remaining = max(0, limit - used)
        warning_threshold = limit * (self.warning_threshold_percent / 100.0)
        is_warning = used >= warning_threshold
        reset_at = self._get_next_reset_time()

        return QuotaStatus(
            user_id=uid,
            plan_code=plan_code.lower().strip() if plan_code else "free",
            limit=limit,
            used=used,
            remaining=remaining,
            reset_at=reset_at,
            is_warning=is_warning,
            can_proceed=True,
        )

    async def refund_quota(
        self,
        user_id: uuid.UUID | str,
        target_date: date | None = None,
    ) -> None:
        """Atomically refund 1 request (decrement with floor 0).

        If Redis is unreachable, logs a warning without crashing.

        Args:
            user_id: Student user UUID.
            target_date: Target UTC date (defaults to current UTC date).
        """
        uid = self._coerce_uuid(user_id)
        key = self.get_quota_key(uid, target_date=target_date)

        try:
            await self.redis.eval(REFUND_LUA_SCRIPT, 1, key)
            logger.debug("Refunded 1 AI quota request for user %s", uid)
        except (RedisError, ConnectionError, TimeoutError, OSError) as exc:
            logger.warning("Failed to refund AI quota for user %s: %s", uid, exc)

    async def reset_quota(
        self,
        user_id: uuid.UUID | str,
        target_date: date | None = None,
    ) -> None:
        """Delete quota key for user (useful for admin overrides and testing).

        Args:
            user_id: Student user UUID.
            target_date: Target UTC date (defaults to current UTC date).
        """
        uid = self._coerce_uuid(user_id)
        key = self.get_quota_key(uid, target_date=target_date)

        try:
            await self.redis.delete(key)
            logger.info("Reset AI quota key '%s'", key)
        except (RedisError, ConnectionError, TimeoutError, OSError) as exc:
            logger.warning("Failed to reset quota key '%s': %s", key, exc)

    @asynccontextmanager
    async def reserve(
        self,
        user_id: uuid.UUID | str,
        plan_code: str = "free",
        target_date: date | None = None,
    ) -> AsyncIterator[QuotaStatus]:
        """Async context manager guaranteeing quota reservation and rollback on failure.

        Reserves quota before yield.
        If an exception is raised inside the context block, automatically refunds
        the reserved quota and re-raises. If block succeeds, reservation is finalized.

        Args:
            user_id: Student user UUID.
            plan_code: Subscription plan tier ('free' or 'pro').
            target_date: Optional target UTC date.

        Yields:
            QuotaStatus: The successful reservation status.

        Raises:
            QuotaExceededError: If daily quota is exceeded.
            QuotaServiceUnavailableError: If Redis is unavailable (fail-closed).
        """
        uid = self._coerce_uuid(user_id)
        status = await self.reserve_quota(user_id=uid, plan_code=plan_code, target_date=target_date)
        try:
            yield status
        except BaseException:
            await self.refund_quota(user_id=uid, target_date=target_date)
            raise


# ---------------------------------------------------------------------------
# Dependency Provider
# ---------------------------------------------------------------------------

_quota_service: AIQuotaService | None = None


def get_quota_service() -> AIQuotaService:
    """FastAPI dependency provider returning shared AIQuotaService instance."""
    global _quota_service
    if _quota_service is None:
        _quota_service = AIQuotaService()
    return _quota_service


__all__ = [
    "DEFAULT_FREE_DAILY_QUOTA",
    "DEFAULT_PRO_DAILY_QUOTA",
    "KEY_TTL_SECONDS",
    "REFUND_LUA_SCRIPT",
    "RESERVE_LUA_SCRIPT",
    "WARNING_THRESHOLD_PERCENT",
    "AIQuotaService",
    "QuotaError",
    "QuotaExceededError",
    "QuotaServiceUnavailableError",
    "QuotaStatus",
    "get_quota_service",
]
