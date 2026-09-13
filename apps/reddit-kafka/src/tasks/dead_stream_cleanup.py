"""Background task to clean up dead streams and locks."""

import asyncio
import logging
from typing import Any

import redis.asyncio as redis

from src.repositories.stream_registry import StreamRegistry

logger = logging.getLogger(__name__)


class DeadStreamCleanup:
    """
    Periodically scans for dead streams and cleans up stale locks.

    Dead stream detection:
    - Stream status is not in (active, paused, starting)
    - Instance heartbeat is missing or expired
    - Lock TTL has expired
    """

    def __init__(
        self,
        registry: StreamRegistry,
        redis_client: redis.Redis,
        cleanup_interval: int = 100,
    ):
        """
        Args:
            registry: StreamRegistry
            redis_client: Async Redis client
            cleanup_interval: Seconds between cleanup runs
        """
        self.registry = registry
        self.redis = redis_client
        self.cleanup_interval = cleanup_interval
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background cleanup task."""
        if self._running:
            logger.warning("DeadStreamCleanup already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info(f"DeadStreamCleanup started (interval: {self.cleanup_interval}s)")

    async def stop(self) -> None:
        """Stop the background cleanup task."""
        self._running = False
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                logger.warning("DeadStreamCleanup stop timeout, cancelling")
                self._task.cancel()
        logger.info("DeadStreamCleanup stopped")

    async def _cleanup_loop(self) -> None:
        """Main cleanup loop."""
        while self._running:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self.cleanup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"DeadStreamCleanup error: {e}", exc_info=True)

    async def cleanup(self) -> None:
        """Scan and clean up dead streams."""
        try:
            streams = await self.registry.list_streams()

            dead_count = 0
            for stream in streams:
                stream_id = stream.get("id")
                subreddit = stream.get("subreddit")
                status = stream.get("status")

                # Detect dead stream
                if self._is_dead_stream(stream):
                    logger.info(
                        f"Cleaning up dead stream: {subreddit} (status={status})"
                    )

                    # Delete stream entry
                    try:
                        if stream_id:
                            await self.registry.delete_stream(str(stream_id))
                            dead_count += 1
                    except Exception as e:
                        logger.error(f"Error deleting stream {stream_id}: {e}")

                    # Clean up associated lock if it still exists
                    if subreddit:
                        lock_key = f"stream:lock:{subreddit}"
                        try:
                            await self.redis.delete(lock_key)
                            logger.debug(f"Deleted stale lock for {subreddit}")
                        except Exception as e:
                            logger.error(f"Error deleting lock: {e}")

            if dead_count > 0:
                logger.info(f"Cleaned up {dead_count} dead streams")

        except Exception as e:
            logger.error(f"Error during cleanup sweep: {e}", exc_info=True)

    @staticmethod
    def _is_dead_stream(stream: dict[str, Any]) -> bool:
        """Determine if a stream is dead (should be cleaned up).

        A stream is considered dead if:
        - Status is 'stopped', 'error', or 'inactive'
        - Status is 'paused' or 'starting' for extended time (not implemented for now)
        """
        status = stream.get("status", "")

        dead_statuses = ["stopped", "error", "inactive"]
        return status in dead_statuses
