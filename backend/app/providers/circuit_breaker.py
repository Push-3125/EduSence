"""Circuit Breaker pattern implementation for EduSense external service calls.

Guards external provider calls (such as Google Gemini API) against cascading failures
and allows graceful recovery using CLOSED, OPEN, and HALF_OPEN states.
"""

from __future__ import annotations

import enum
import inspect
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger("edusense.providers.circuit_breaker")

T = TypeVar("T")


class CircuitState(str, enum.Enum):
    """States of the circuit breaker."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenError(Exception):
    """Raised when an operation is rejected because the circuit is OPEN."""

    def __init__(
        self,
        message: str = "Circuit breaker is OPEN. Calls are temporarily blocked.",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CircuitBreaker:
    """Thread-safe circuit breaker implementation supporting sync and async callables."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_success_threshold: int = 2,
    ) -> None:
        """Initialize the circuit breaker.

        Args:
            failure_threshold: Number of consecutive failures in CLOSED state before opening.
            recovery_timeout: Seconds to wait in OPEN state before transitioning to HALF_OPEN.
            half_open_success_threshold: Consecutive successful calls in HALF_OPEN to close circuit.
        """
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout = max(0.0, float(recovery_timeout))
        self.half_open_success_threshold = max(1, half_open_success_threshold)

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float | None = None
        self._lock = threading.Lock()

    def _check_and_update_state(self) -> CircuitState:
        """Evaluate state transitions based on time elapsed while holding self._lock."""
        if self._state == CircuitState.OPEN and self._last_failure_time is not None:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed > self.recovery_timeout:
                logger.info(
                    "Circuit breaker transitioning OPEN -> HALF_OPEN (%.1fs elapsed > %.1fs recovery timeout)",
                    elapsed,
                    self.recovery_timeout,
                )
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
        return self._state

    def _before_call(self) -> None:
        """Pre-execution check. Raises CircuitBreakerOpenError if the circuit is OPEN."""
        with self._lock:
            state = self._check_and_update_state()
            if state == CircuitState.OPEN:
                now = time.monotonic()
                elapsed = now - (self._last_failure_time or now)
                retry_after = max(0.0, self.recovery_timeout - elapsed)
                raise CircuitBreakerOpenError(
                    f"Circuit breaker is OPEN. Calls blocked for another {retry_after:.1f}s",
                    retry_after=retry_after,
                )

    def _on_success(self) -> None:
        """Post-execution hook called after a successful operation."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.half_open_success_threshold:
                    logger.info(
                        "Circuit breaker transitioning HALF_OPEN -> CLOSED (success threshold %d reached)",
                        self.half_open_success_threshold,
                    )
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
                    self._last_failure_time = None
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    def _on_failure(self) -> None:
        """Post-execution hook called after an operation raises an unhandled exception."""
        with self._lock:
            now = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                logger.warning(
                    "Circuit breaker call failed in HALF_OPEN state -> transitioning back to OPEN"
                )
                self._state = CircuitState.OPEN
                self._last_failure_time = now
                self._success_count = 0
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.failure_threshold:
                    logger.warning(
                        "Circuit breaker failure threshold (%d) reached -> transitioning CLOSED -> OPEN",
                        self.failure_threshold,
                    )
                    self._state = CircuitState.OPEN
                    self._last_failure_time = now
            elif self._state == CircuitState.OPEN:
                self._last_failure_time = now

    def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Synchronously execute func with circuit breaker protection.

        Args:
            func: Target callable to execute.
            *args: Positional arguments for func.
            **kwargs: Keyword arguments for func.

        Returns:
            Return value of func.

        Raises:
            CircuitBreakerOpenError: If the circuit is currently OPEN.
            Exception: Any exception raised by func (recorded as failure).
        """
        self._before_call()
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    async def acall(self, coro_func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Asynchronously execute coro_func with circuit breaker protection.

        Args:
            coro_func: Target async callable or coroutine.
            *args: Positional arguments for coro_func.
            **kwargs: Keyword arguments for coro_func.

        Returns:
            Awaited return value of coro_func.

        Raises:
            CircuitBreakerOpenError: If the circuit is currently OPEN.
            Exception: Any exception raised by coro_func (recorded as failure).
        """
        self._before_call()
        try:
            if inspect.iscoroutine(coro_func):
                result = await coro_func
            else:
                result = await coro_func(*args, **kwargs)
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    def reset(self) -> None:
        """Reset the circuit breaker back to initial CLOSED state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None

    @property
    def state(self) -> CircuitState:
        """Return the current circuit state, updating if recovery timeout has elapsed."""
        with self._lock:
            return self._check_and_update_state()

    @property
    def failure_count(self) -> int:
        """Return the current consecutive failure count."""
        with self._lock:
            return self._failure_count

    @property
    def success_count(self) -> int:
        """Return the current consecutive success count (in HALF_OPEN state)."""
        with self._lock:
            return self._success_count

    @property
    def last_failure_time(self) -> float | None:
        """Return the monotonic timestamp of the last recorded failure."""
        with self._lock:
            return self._last_failure_time
