"""Distributed lock manager for preventing duplicate streams across instances."""

import logging
import uuid
from collections.abc import Awaitable
from typing import cast

import redis.asyncio as redis

logger = logging.getLogger(__name__)

_REFRESH_IF_OWNER_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
end
return 0
"""

_DELETE_IF_OWNER_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


class DistributedLockManager:
    """
    Manages distributed locks using Redis with TTL.

    Prevents multiple instances from starting the same stream (identified by subreddit).
    Uses SET NX EX pattern for atomic lock acquisition and TTL.
    """

    def __init__(self, redis_client: redis.Redis):
        """
        Args:
            redis_client: Async Redis client
        """
        self.redis = redis_client

    def _lock_key(self, subreddit: str) -> str:
        """Generate Redis key for lock."""
        return f"stream:lock:{subreddit}"

    async def acquire_lock(
        self,
        subreddit: str,
        instance_id: str,
        ttl: int = 60,
    ) -> str | None:
        """Acquire a distributed lock for a subreddit.

        Args:
            subreddit: Subreddit name (used as lock identifier)
            instance_id: Unique identifier of this instance
            ttl: Lock TTL in seconds (default 60s)

        Returns:
            An opaque ownership token if acquired, otherwise None. The caller must
            use this exact token to refresh or release the lease.
        """
        key = self._lock_key(subreddit)
        owner_token = f"{instance_id}:{uuid.uuid4()}"

        acquired = await self.redis.set(
            key,
            owner_token,
            nx=True,
            ex=ttl,
        )

        if acquired:
            logger.debug(f"✓ Acquired lock for {subreddit} (instance={instance_id})")
            return owner_token
        else:
            holder = await self.redis.get(key)
            logger.warning(
                f"✗ Lock already held for {subreddit} by {holder or 'unknown'}"
            )
            return None

    async def refresh_lock(
        self,
        subreddit: str,
        owner_token: str,
        ttl: int = 60,
    ) -> bool:
        """Refresh an existing lock (extend TTL).

        Args:
            subreddit: Subreddit name
            owner_token: Exact opaque token returned by acquire_lock
            ttl: New TTL in seconds

        Returns:
            True if refreshed successfully, False if lock token doesn't match
        """
        key = self._lock_key(subreddit)
        success = await cast(
            Awaitable[int],
            self.redis.eval(
                _REFRESH_IF_OWNER_SCRIPT,
                1,
                key,
                owner_token,
                ttl,
            ),
        )
        if success:
            logger.debug(f"✓ Refreshed lock for {subreddit}")
        else:
            logger.warning(f"✗ Cannot refresh unowned or expired lock for {subreddit}")
        return bool(success)

    async def release_lock(self, subreddit: str, owner_token: str) -> bool:
        """Release a lock (delete from Redis).

        Args:
            subreddit: Subreddit name
            owner_token: Exact opaque token returned by acquire_lock

        Returns:
            True if released, False if not held by this instance
        """
        key = self._lock_key(subreddit)
        deleted = await cast(
            Awaitable[int],
            self.redis.eval(
                _DELETE_IF_OWNER_SCRIPT,
                1,
                key,
                owner_token,
            ),
        )
        if deleted:
            logger.debug(f"✓ Released lock for {subreddit}")
        else:
            logger.warning(f"✗ Cannot release unowned or expired lock for {subreddit}")
        return bool(deleted)

    async def is_locked(self, subreddit: str) -> bool:
        """Check if a lock currently exists.

        Args:
            subreddit: Subreddit name

        Returns:
            True if locked, False otherwise
        """
        key = self._lock_key(subreddit)
        exists = await self.redis.exists(key)
        return bool(exists)

    async def get_lock_holder(self, subreddit: str) -> str | None:
        """Get the current lock holder.

        Args:
            subreddit: Subreddit name

        Returns:
            Lock holder info (format: instance_id:token), or None if not locked
        """
        key = self._lock_key(subreddit)
        result = await self.redis.get(key)
        return str(result) if result else None
