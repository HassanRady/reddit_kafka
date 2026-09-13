import os

import pytest
import redis.asyncio as redis

from src.stream.lock import DistributedLockManager

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("RUN_E2E") != "1",
        reason="run through scripts/run-e2e.sh",
    ),
]


@pytest.mark.asyncio
async def test_stale_owner_cannot_release_or_refresh_reacquired_lock() -> None:
    client = redis.Redis(
        host="127.0.0.1",
        port=int(os.getenv("E2E_REDIS_PORT", "16379")),
        username="local",
        password="password",
        decode_responses=True,
    )
    manager = DistributedLockManager(client)
    key = "stream:lock:e2e_atomic_release"

    try:
        stale_token = await manager.acquire_lock(
            "e2e_atomic_release", "process-1", ttl=60
        )
        assert stale_token is not None

        # Model expiry followed by acquisition by a successor before the stale
        # owner attempts its release and refresh operations.
        successor_token = "process-2:successor-lease"
        await client.set(key, successor_token, ex=60)

        assert await manager.release_lock("e2e_atomic_release", stale_token) is False
        assert await client.get(key) == successor_token

        assert (
            await manager.refresh_lock("e2e_atomic_release", stale_token, ttl=120)
            is False
        )
        assert await client.get(key) == successor_token

        assert await manager.release_lock("e2e_atomic_release", successor_token) is True
        assert await client.exists(key) == 0
    finally:
        await client.delete(key)
        await client.aclose()
