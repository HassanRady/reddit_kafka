from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

_default_session_maker: Callable[[], Any] | None = None

with suppress(Exception):
    # Import async session provider lazily to avoid circular imports in tests
    from src.db import get_session as _default_session_maker


class StreamExistsError(Exception):
    """Raised when attempting to create a stream that already exists."""


class StreamNotFoundError(Exception):
    """Raised when a stream cannot be found in the registry."""


def _now_iso() -> str:
    """Return UTC timestamp as ISO8601 string (naive, no timezone offset).

    Uses modern timezone-aware approach then strips timezone for DB compatibility.
    """
    return datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"


class StreamRegistry:
    """Small helper to manage stream metadata/checkpoints in Redis.

    Key layout used here:
      - stream:meta:{stream_id} -> hash with metadata
        (subreddit, status, instance_id, config...)
      - stream:subreddit:{subreddit} -> stream_id (to detect duplicates)
      - stream:checkpoint:{stream_id} -> hash
        (last_comment_id, last_processed_at)
    """

    def __init__(
        self, redis: Any, session_maker: Callable[[], Any] | None = None
    ) -> None:
        """Create a StreamRegistry.

        Args:
            redis: Async Redis client
            session_maker: Optional async session provider (callable) used to persist
                stream metadata to Postgres. If None, DB persistence is disabled.
        """
        self._redis = redis
        self._session_maker = session_maker or _default_session_maker

    @staticmethod
    def _meta_key(stream_id: str) -> str:
        return f"stream:meta:{stream_id}"

    @staticmethod
    def _subreddit_key(subreddit: str) -> str:
        return f"stream:subreddit:{subreddit}"

    @staticmethod
    def _checkpoint_key(stream_id: str) -> str:
        return f"stream:checkpoint:{stream_id}"

    @staticmethod
    def _stop_request_key(stream_id: str) -> str:
        return f"stream:stop-request:{stream_id}"

    async def create_stream(
        self,
        subreddit: str,
        config: dict[str, Any] | None = None,
        instance_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new stream if not exists.

        Raises StreamExistsError if a stream for the subreddit already exists.
        Returns the created stream metadata dict.
        """
        config = config or {}
        redis = self._redis
        sub_key = self._subreddit_key(subreddit)

        stream_id = str(uuid.uuid4())
        claimed = await redis.set(sub_key, stream_id, nx=True)
        if not claimed:
            # someone else already has a stream for this subreddit
            existing = await redis.get(sub_key)
            raise StreamExistsError(
                f"stream already exists for subreddit={subreddit} (id={existing})"
            )

        meta = {
            "id": stream_id,
            "subreddit": subreddit,
            "status": "starting",
            "instance_id": instance_id or "",
            "config": json.dumps(config),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        await redis.hset(self._meta_key(stream_id), mapping=meta)

        # Track stream ID in a set for efficient registry listing
        await redis.sadd("streams:all", stream_id)

        # Persist to Postgres streams table if session maker provided
        if self._session_maker is not None:
            try:
                async with self._session_maker() as session:
                    stmt = text(
                        """
                        INSERT INTO streams (
                            id,
                            subreddit,
                            status,
                            instance_id,
                            config,
                            created_at,
                            updated_at
                        )
                        VALUES (
                            :id,
                            :subreddit,
                            :status,
                            :instance_id,
                            :config,
                            NOW(),
                            NOW()
                        )
                        ON CONFLICT (id) DO NOTHING
                        """
                    )
                    await session.execute(
                        stmt,
                        {
                            "id": stream_id,
                            "subreddit": subreddit,
                            "status": meta["status"],
                            "instance_id": instance_id or None,
                            "config": json.dumps(config),
                        },
                    )
                    await session.commit()
            except Exception:
                logger.exception("Failed to persist stream metadata to Postgres")

        return meta

    async def get_stream(self, stream_id: str) -> dict[str, Any]:
        data: dict[str, Any] = await self._redis.hgetall(self._meta_key(stream_id))
        if not data:
            raise StreamNotFoundError(stream_id)
        if data.get("config"):
            try:
                data["config"] = json.loads(data["config"])
            except Exception:
                data["config"] = {}
        return data

    async def get_stream_by_subreddit(self, subreddit: str) -> dict[str, Any] | None:
        sid = await self._redis.get(self._subreddit_key(subreddit))
        if not sid:
            return None
        return await self.get_stream(sid)

    async def list_streams(self) -> list[dict[str, Any]]:
        redis = self._redis
        cursor = 0
        results: list[dict[str, Any]] = []
        pattern = "stream:meta:*"
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                pipe = redis.pipeline()
                for k in keys:
                    pipe.hgetall(k)
                rows = await pipe.execute()
                for data in rows:
                    if data:
                        if data.get("config"):
                            try:
                                data["config"] = json.loads(data["config"])
                            except Exception:
                                data["config"] = {}
                        results.append(data)
            if cursor == 0:
                break
        return results

    async def update_status(
        self, stream_id: str, status: str, instance_id: str | None = None
    ) -> None:
        mapping = {"status": status, "updated_at": _now_iso()}
        if instance_id is not None:
            mapping["instance_id"] = instance_id
        await self._redis.hset(self._meta_key(stream_id), mapping=mapping)
        # Also update Postgres if session maker available
        if self._session_maker is not None:
            try:
                async with self._session_maker() as session:
                    stmt = text(
                        """
                        UPDATE streams
                        SET status = :status,
                            instance_id = :instance_id,
                            updated_at = NOW()
                        WHERE id = :id
                        """
                    )
                    await session.execute(
                        stmt,
                        {
                            "status": status,
                            "instance_id": instance_id,
                            "id": stream_id,
                        },
                    )
                    await session.commit()
            except Exception:
                logger.exception("Failed to update stream status in Postgres")

    async def request_stop(self, stream_id: str) -> None:
        """Persist a stop request that can be observed by another process."""
        meta = await self.get_stream(stream_id)
        await self._redis.set(self._stop_request_key(stream_id), _now_iso())
        await self.update_status(
            stream_id,
            "stopping",
            instance_id=meta.get("instance_id") or None,
        )

    async def is_stop_requested(self, stream_id: str) -> bool:
        """Return whether a shared stop request exists for the stream."""
        return bool(await self._redis.exists(self._stop_request_key(stream_id)))

    async def delete_stream(self, stream_id: str) -> None:
        meta = await self._redis.hgetall(self._meta_key(stream_id))
        if not meta:
            return
        subreddit = meta.get("subreddit")
        pipe = self._redis.pipeline()
        pipe.delete(self._meta_key(stream_id))
        pipe.delete(self._checkpoint_key(stream_id))
        pipe.delete(self._stop_request_key(stream_id))
        if subreddit:
            pipe.delete(self._subreddit_key(subreddit))
        # Remove from stream tracking set
        pipe.srem("streams:all", stream_id)
        await pipe.execute()

    async def set_checkpoint(
        self,
        stream_id: str,
        last_comment_id: str | None = None,
        last_processed_at: str | None = None,
    ) -> None:
        key = self._checkpoint_key(stream_id)
        mapping: dict[str, str] = {}
        if last_comment_id is not None:
            mapping["last_comment_id"] = last_comment_id
        if last_processed_at is not None:
            mapping["last_processed_at"] = last_processed_at
        if mapping:
            mapping["updated_at"] = _now_iso()
            await self._redis.hset(key, mapping=mapping)

    async def get_checkpoint(self, stream_id: str) -> dict[str, str | None]:
        data = await self._redis.hgetall(self._checkpoint_key(stream_id))
        return {
            "last_comment_id": data.get("last_comment_id"),
            "last_processed_at": data.get("last_processed_at"),
            "updated_at": data.get("updated_at"),
        }
