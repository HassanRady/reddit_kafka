import uuid
from unittest.mock import AsyncMock

import pytest

from src.stream.lock import (
    _DELETE_IF_OWNER_SCRIPT,
    _REFRESH_IF_OWNER_SCRIPT,
    DistributedLockManager,
)


@pytest.mark.asyncio
async def test_acquire_returns_the_exact_stored_owner_token(redis_mock) -> None:
    redis_mock.set = AsyncMock(return_value=True)
    manager = DistributedLockManager(redis_mock)

    owner_token = await manager.acquire_lock("python", "process-1", ttl=60)

    assert owner_token is not None
    instance_id, unique_token = owner_token.split(":", maxsplit=1)
    assert instance_id == "process-1"
    assert uuid.UUID(unique_token)
    redis_mock.set.assert_awaited_once_with(
        "stream:lock:python",
        owner_token,
        nx=True,
        ex=60,
    )


@pytest.mark.asyncio
async def test_release_uses_atomic_exact_token_comparison(redis_mock) -> None:
    redis_mock.eval = AsyncMock(return_value=0)
    manager = DistributedLockManager(redis_mock)
    stale_token = "process-1:old-lease"

    released = await manager.release_lock("python", stale_token)

    assert released is False
    redis_mock.eval.assert_awaited_once_with(
        _DELETE_IF_OWNER_SCRIPT,
        1,
        "stream:lock:python",
        stale_token,
    )
    redis_mock.get.assert_not_awaited()
    redis_mock.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_uses_atomic_exact_token_comparison(redis_mock) -> None:
    redis_mock.eval = AsyncMock(return_value=1)
    manager = DistributedLockManager(redis_mock)
    owner_token = "process-1:current-lease"

    refreshed = await manager.refresh_lock("python", owner_token, ttl=45)

    assert refreshed is True
    redis_mock.eval.assert_awaited_once_with(
        _REFRESH_IF_OWNER_SCRIPT,
        1,
        "stream:lock:python",
        owner_token,
        45,
    )
    redis_mock.get.assert_not_awaited()
    redis_mock.expire.assert_not_awaited()
