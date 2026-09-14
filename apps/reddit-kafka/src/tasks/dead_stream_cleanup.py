"""Background task to retire terminal stream registry entries safely."""

import asyncio
import logging
from typing import Any

import redis.asyncio as redis

from src.repositories.stream_registry import StreamRegistry

logger = logging.getLogger(__name__)


class DeadStreamCleanup:
    """
    Periodically removes ephemeral registry state for terminal streams.

    Dead stream detection:
    - Stream status is stopped, error, or inactive

    Checkpoints and stable stream identities remain available for restarts. Lock
    leases are never mutated here; owners release them and abandoned leases expire.
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

                    # Delete only the exact dead snapshot observed by this scan.
                    # A concurrent restart changes status/updated_at and makes the
                    # conditional delete a no-op. Checkpoints and stable identity
                    # are retained so a later start resumes the same logical stream.
                    try:
                        if stream_id:
                            deleted = await self.registry.delete_stream(
                                str(stream_id),
                                expected_status=str(status) if status else None,
                                expected_updated_at=(
                                    str(stream.get("updated_at"))
                                    if stream.get("updated_at")
                                    else None
                                ),
                            )
                            if deleted:
                                dead_count += 1
                            else:
                                logger.info(
                                    "Skipped cleanup for stream %s because it changed",
                                    stream_id,
                                )
                    except Exception as e:
                        logger.error(f"Error deleting stream {stream_id}: {e}")

                    # Locks are leases: only the exact token owner may release one,
                    # and abandoned locks expire through their TTL. Cleanup must not
                    # delete a lock that may belong to a successor worker.

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
