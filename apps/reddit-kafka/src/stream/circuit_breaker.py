"""Circuit breaker pattern for handling Reddit API rate limits and transient errors."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from src.stream.error_handler import ErrorHandler

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Too many failures, don't try
    HALF_OPEN = "half_open"  # Testing if ready to retry


class CircuitBreaker:
    """
    Netflix Hystrix-style circuit breaker.

    Prevents cascading failures by failing fast when:
    - Too many consecutive failures
    - Service is recovering (half-open recovery timeout)

    Exponential backoff on rate limits.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        success_threshold: int = 2,
        backoff_multiplier: float = 2.0,
        max_backoff: int = 3600,  # 1 hour
    ):
        """
        Args:
            failure_threshold: Failures before opening circuit
            recovery_timeout: Seconds to wait before half-open (initial)
            success_threshold: Successes in half-open before closing
            backoff_multiplier: Multiplier for exponential backoff
            max_backoff: Maximum backoff in seconds
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        self.backoff_multiplier = backoff_multiplier
        self.max_backoff = max_backoff

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: datetime | None = None
        self.backoff_seconds = recovery_timeout
        self._lock = asyncio.Lock()

    async def call(self, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        """Execute a coroutine through the circuit breaker.

        Args:
            coro_factory: Zero-argument callable that returns the coroutine to run

        Returns:
            Result of the coroutine

        Raises:
            RuntimeError: If circuit is open
        """
        async with self._lock:
            if self.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    logger.info(
                        "Circuit breaker entering HALF_OPEN (backoff=%ss)",
                        self.backoff_seconds,
                    )
                else:
                    raise RuntimeError(
                        f"Circuit breaker OPEN. Retry in {self._time_until_retry()}s"
                    )

        try:
            result = await coro_factory()
            async with self._lock:
                self._record_success()
            return result
        except Exception as e:
            async with self._lock:
                self._record_failure(e)
            raise

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to try half-open."""
        if not self.last_failure_time:
            return True
        elapsed = (datetime.now(UTC) - self.last_failure_time).total_seconds()
        return elapsed >= self.backoff_seconds

    def _time_until_retry(self) -> int:
        """Time remaining until retry is allowed (in seconds)."""
        if not self.last_failure_time:
            return 0
        elapsed = (datetime.now(UTC) - self.last_failure_time).total_seconds()
        return max(0, int(self.backoff_seconds - elapsed))

    def _record_success(self) -> None:
        """Record a successful call."""
        self.failure_count = 0

        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                self.state = CircuitState.CLOSED
                self.backoff_seconds = self.recovery_timeout
                logger.info("✓ Circuit breaker CLOSED (recovered)")
        elif self.state == CircuitState.CLOSED:
            logger.debug("Circuit breaker: operation successful")

    def _record_failure(self, exc: Exception) -> None:
        """Record a failed call."""
        self.failure_count += 1
        self.last_failure_time = datetime.now(UTC)
        self.success_count = 0
        # Determine suggested backoff from error handler (if applicable)
        try:
            suggested = ErrorHandler.get_backoff_duration(exc)
        except Exception:
            suggested = 0

        if self.state == CircuitState.HALF_OPEN:
            # Failure during recovery attempt: increase backoff and reopen.
            new_backoff = int(
                min(
                    max(self.backoff_seconds * self.backoff_multiplier, suggested),
                    self.max_backoff,
                )
            )
            self.backoff_seconds = new_backoff
            self.state = CircuitState.OPEN
            logger.error(
                f"Circuit breaker OPEN (exponential backoff: {self.backoff_seconds}s). "
                f"Reason: {exc}"
            )
        elif self.failure_count >= self.failure_threshold:
            # Open circuit on threshold and prefer any error-suggested backoff
            new_backoff = int(
                min(
                    max(self.backoff_seconds * self.backoff_multiplier, suggested),
                    self.max_backoff,
                )
            )
            self.backoff_seconds = new_backoff
            self.state = CircuitState.OPEN
            logger.error(
                f"Circuit breaker OPEN after {self.failure_count} failures. "
                f"Initial backoff: {self.backoff_seconds}s. Reason: {exc}"
            )

    @property
    def fail_count(self) -> int:
        """Current failure count."""
        return self.failure_count

    def is_open(self) -> bool:
        """Check if circuit is currently open."""
        return self.state == CircuitState.OPEN

    def reset(self) -> None:
        """Force reset the circuit breaker."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.backoff_seconds = self.recovery_timeout
        self.last_failure_time = None
        logger.info("Circuit breaker manually reset")

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(state={self.state.value}, "
            f"failures={self.failure_count}/{self.failure_threshold}, "
            f"backoff={self.backoff_seconds}s)"
        )
