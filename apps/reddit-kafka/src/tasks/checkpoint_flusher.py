"""Periodic task to flush Redis stream state to Postgres."""

import asyncio
import logging
import uuid
from collections.abc import Awaitable
from datetime import datetime
from typing import Any, cast

import redis.asyncio as redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class CheckpointFlusher:
    """Flushes checkpoint data from Redis to Postgres every N seconds."""

    def __init__(
        self,
        redis_client: redis.Redis,
        session_maker: async_sessionmaker[AsyncSession],
        instance_id: str,
        flush_interval: int = 10,
    ) -> None:
        """
        Args:
            redis_client: Async Redis client
            session_maker: SQLAlchemy async session factory
            instance_id: ID of the application instance whose streams are flushed
            flush_interval: Seconds between flushes (default: 10s)
        """
        self.redis = redis_client
        self.session_maker = session_maker
        self.instance_id = instance_id
        self.stream_index_key = f"streams:instance:{instance_id}"
        self.flush_interval = flush_interval
        self._running = False
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        if self._running:
            logger.warning("CheckpointFlusher already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._flush_loop())
        logger.info(f"CheckpointFlusher started (interval: {self.flush_interval}s)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                logger.warning("CheckpointFlusher stop timeout, cancelling")
                self._task.cancel()
        logger.info("CheckpointFlusher stopped")

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.flush_interval)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"CheckpointFlusher error: {e}", exc_info=True)

    def _parse_checkpoint_data(
        self, checkpoint_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Parse and normalize checkpoint data from Redis.

        Returns normalized checkpoint dict or None if parsing fails.
        """
        try:
            last_processed_at = None
            last_processed_at_str = checkpoint_data.get("last_processed_at")
            if last_processed_at_str:
                # Parse ISO8601 timestamp and ensure it's naive UTC
                dt = datetime.fromisoformat(last_processed_at_str.rstrip("Z"))
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                last_processed_at = dt

            return {
                "last_comment_id": checkpoint_data.get("last_comment_id"),
                "last_processed_at": last_processed_at,
            }
        except Exception as e:
            logger.error(f"Error parsing checkpoint data: {e}", exc_info=True)
            return None

    async def flush(self) -> None:
        try:
            stream_ids = await self.redis.smembers(self.stream_index_key)  # type: ignore
            if not stream_ids:
                return

            checkpoints_to_upsert: list[dict[str, Any]] = []
            stale_stream_ids: list[str] = []

            # Fetch ownership metadata and checkpoints in one Redis round trip.
            pipe = self.redis.pipeline()
            for stream_id in stream_ids:
                pipe.hgetall(f"stream:meta:{stream_id}")
                pipe.hgetall(f"stream:checkpoint:{stream_id}")

            results = await pipe.execute()

            for index, stream_id in enumerate(stream_ids):
                meta = results[index * 2]
                checkpoint_data = results[index * 2 + 1]

                # Ownership can move between instances. Discard stale index
                # entries instead of allowing an old instance to flush them.
                if not meta or meta.get("instance_id") != self.instance_id:
                    stale_stream_ids.append(stream_id)
                    continue

                if not checkpoint_data:
                    continue

                parsed = self._parse_checkpoint_data(checkpoint_data)
                if parsed is None:
                    continue

                checkpoints_to_upsert.append(
                    {
                        "id": stream_id,
                        "stream_id": stream_id,
                        "last_comment_id": parsed["last_comment_id"],
                        "last_processed_at": parsed["last_processed_at"],
                    }
                )

            if stale_stream_ids:
                await cast(
                    Awaitable[int],
                    self.redis.srem(self.stream_index_key, *stale_stream_ids),
                )

            if checkpoints_to_upsert:
                await self._upsert_checkpoints(checkpoints_to_upsert)

        except Exception as e:
            logger.error(f"CheckpointFlusher.flush error: {e}", exc_info=True)

    async def _upsert_checkpoints(self, checkpoints: list[dict[str, Any]]) -> None:
        """Batch upsert checkpoints to Postgres."""
        async with self.session_maker() as session:
            try:
                stmt = """
                    INSERT INTO stream_checkpoints (
                        id,
                        stream_id,
                        last_comment_id,
                        last_processed_at,
                        updated_at
                    )
                    VALUES (
                        :id,
                        :stream_id,
                        :last_comment_id,
                        :last_processed_at,
                        NOW()
                    )
                    ON CONFLICT (stream_id) DO UPDATE SET
                        last_comment_id = EXCLUDED.last_comment_id,
                        last_processed_at = EXCLUDED.last_processed_at,
                        updated_at = NOW()
                """

                parameters = [
                    {
                        "id": checkpoint.get("id") or str(uuid.uuid4()),
                        "stream_id": checkpoint["stream_id"],
                        "last_comment_id": checkpoint.get("last_comment_id"),
                        "last_processed_at": checkpoint.get("last_processed_at"),
                    }
                    for checkpoint in checkpoints
                ]
                await session.execute(text(stmt), parameters)

                await session.commit()
                logger.debug(f"Flushed {len(checkpoints)} checkpoints to Postgres")

            except Exception as e:
                await session.rollback()
                logger.error(f"Error upserting checkpoints: {e}", exc_info=True)
                raise

    async def flush_stream_on_stop(self, stream_id: str) -> None:
        try:
            key = f"stream:checkpoint:{stream_id}"
            checkpoint_data: dict[str, Any] = await self.redis.hgetall(key)  # type: ignore

            if checkpoint_data:
                last_comment_id = checkpoint_data.get("last_comment_id")
                last_processed_at_str = checkpoint_data.get("last_processed_at")

                last_processed_at = None
                if last_processed_at_str:
                    # Parse ISO8601 timestamp and ensure it's naive UTC
                    dt = datetime.fromisoformat(last_processed_at_str.rstrip("Z"))
                    if dt.tzinfo is not None:
                        dt = dt.replace(tzinfo=None)
                    last_processed_at = dt

                await self._upsert_checkpoints(
                    [
                        {
                            "id": stream_id,
                            "stream_id": stream_id,
                            "last_comment_id": last_comment_id,
                            "last_processed_at": last_processed_at,
                        }
                    ]
                )

                logger.debug(f"Immediately flushed checkpoint for {stream_id}")
        except Exception as e:
            logger.error(
                f"Error flushing stream {stream_id} on stop: {e}", exc_info=True
            )
