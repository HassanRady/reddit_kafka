import json
import logging
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis
from aiohttp import ClientConnectionError
from asyncprawcore.exceptions import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NotFound,
    OAuthException,
    RequestException,
    ResponseException,
    TooManyRequests,
)
from sqlalchemy import text

from src.stream.exceptions import KafkaDeliveryError

_default_session_maker: Callable[[], Any] | None = None

with suppress(Exception):
    from src.db import get_session as _default_session_maker

logger = logging.getLogger(__name__)

_FATAL_EXCEPTIONS = (
    BadRequest,
    Forbidden,
    InvalidToken,
    NotFound,
    OAuthException,
    ValueError,
    KafkaDeliveryError,
)
_BACKOFF_EXCEPTIONS = (TooManyRequests,)
_RETRYABLE_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    ClientConnectionError,
    RequestException,
    ResponseException,
)


class ErrorHandler:
    """
    Tracks errors and determines recovery strategies.

    Stores errors in Redis for short-term tracking (configurable TTL).
    Logs errors to help debugging.
    """

    def __init__(self, redis_client: redis.Redis, session_maker: Any | None = None):
        """
        Args:
            redis_client: Async Redis client
            session_maker: optional async session provider for persisting errors to
                Postgres
        """
        self.redis = redis_client
        self.error_ttl = 86400  # 24 hours
        self._session_maker = session_maker or _default_session_maker

    def _error_key(self, stream_id: str) -> str:
        """Generate Redis key for error tracking."""
        return f"stream:errors:{stream_id}"

    async def record_error(
        self,
        stream_id: str,
        error_type: str,
        error_message: str,
        is_recoverable: bool = True,
    ) -> None:
        """Record an error in Redis and optionally persist to Postgres.

        Args:
            stream_id: Stream ID
            error_type: Exception class name (e.g., 'TooManyRequests')
            error_message: Exception message
            is_recoverable: Whether this error is recoverable (affects backoff)
        """
        key = self._error_key(stream_id)

        # Use naive UTC timestamp format (compatible with Postgres NOW())
        ts = datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"
        error_entry = {
            "id": str(uuid.uuid4()),
            "error_type": error_type,
            "error_message": error_message,
            "is_recoverable": "1" if is_recoverable else "0",
            "timestamp": ts,
        }

        # Push to Redis list (keep last 100 errors)
        await self.redis.lpush(key, json.dumps(error_entry))  # type: ignore  # Add to front
        await self.redis.ltrim(key, 0, 99)  # type: ignore  # Keep last 100
        await self.redis.expire(key, self.error_ttl)

        logger.warning(
            f"Stream {stream_id}: {error_type} - {error_message} "
            f"(recoverable={is_recoverable})"
        )

        # Persist to Postgres stream_errors table if session maker provided
        if self._session_maker is not None:
            try:
                async with self._session_maker() as session:
                    stmt = text(
                        """
                        INSERT INTO stream_errors (
                            id,
                            stream_id,
                            error_type,
                            error_message,
                            retry_count,
                            is_recoverable,
                            timestamp
                        )
                        VALUES (
                            :id,
                            :stream_id,
                            :error_type,
                            :error_message,
                            :retry_count,
                            :is_recoverable,
                            :timestamp
                        )
                        """
                    )
                    await session.execute(
                        stmt,
                        {
                            "id": error_entry["id"],
                            "stream_id": stream_id,
                            "error_type": error_type,
                            "error_message": error_message,
                            "retry_count": 0,
                            "is_recoverable": 1 if is_recoverable else 0,
                            "timestamp": ts,
                        },
                    )
                    await session.commit()
            except Exception:
                logger.exception("Failed to persist stream error to Postgres")

    async def get_error_count(self, stream_id: str) -> int:
        """Get number of recent errors for a stream.

        Args:
            stream_id: Stream ID

        Returns:
            Number of errors in the last 24 hours
        """
        key = self._error_key(stream_id)
        result = await self.redis.llen(key)  # type: ignore
        return int(result) if result else 0

    async def clear_errors(self, stream_id: str) -> None:
        """Clear error history for a stream.

        Args:
            stream_id: Stream ID
        """
        key = self._error_key(stream_id)
        await self.redis.delete(key)
        logger.debug(f"Cleared error history for stream {stream_id}")

    async def get_recent_errors(
        self, stream_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get recent errors for a stream.

        Args:
            stream_id: Stream ID
            limit: Number of recent errors to return

        Returns:
            List of error entries
        """
        key = self._error_key(stream_id)
        error_strings: list[Any] = await self.redis.lrange(key, 0, limit - 1)  # type: ignore
        return [
            json.loads(str(e)) for e in error_strings if e
        ]  # Convert string back to dict

    @staticmethod
    def is_retryable(error: Exception) -> bool:
        """Check if an error is transient and retryable.

        Args:
            error: Exception instance

        Returns:
            True if retryable, False if fatal
        """
        return not isinstance(error, _FATAL_EXCEPTIONS) and isinstance(
            error, _RETRYABLE_EXCEPTIONS
        )

    @staticmethod
    def should_backoff(error: Exception) -> bool:
        """Check if error requires backoff (rate limit, not generic error).

        Args:
            error: Exception instance

        Returns:
            True if backoff is needed
        """
        return isinstance(error, _BACKOFF_EXCEPTIONS)

    @staticmethod
    def get_backoff_duration(error: Exception) -> int:
        """Get recommended backoff duration for error.

        Args:
            error: Exception instance

        Returns:
            Recommended backoff in seconds
        """
        if isinstance(error, TooManyRequests):
            return 60  # Start with 60s, exponential backoff will increase
        return 5  # Default short backoff for network errors


class RecoveryStrategy:
    """
    Determines recovery action based on error type and context.
    """

    @staticmethod
    def should_retry_immediately(error: Exception) -> bool:
        """Determine if error warrants immediate retry."""
        retryable = ErrorHandler.is_retryable(error)
        is_rate_limit = ErrorHandler.should_backoff(error)
        return retryable and not is_rate_limit

    @staticmethod
    def should_retry_with_backoff(error: Exception) -> bool:
        """Determine if error warrants retry with backoff."""
        return ErrorHandler.should_backoff(error)

    @staticmethod
    def should_abandon_stream(error: Exception) -> bool:
        """Determine if error is fatal and stream should be abandoned."""
        return isinstance(error, _FATAL_EXCEPTIONS)
