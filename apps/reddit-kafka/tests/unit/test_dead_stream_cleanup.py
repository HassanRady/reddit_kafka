from unittest.mock import AsyncMock

import pytest

from src.tasks.dead_stream_cleanup import DeadStreamCleanup


@pytest.mark.asyncio
async def test_cleanup_preserves_checkpoint_and_does_not_delete_lock() -> None:
    dead_stream = {
        "id": "stream-1",
        "subreddit": "python",
        "status": "stopped",
        "updated_at": "2026-05-05T10:00:00Z",
    }
    registry = AsyncMock()
    registry.list_streams.return_value = [dead_stream]
    registry.delete_stream.return_value = True
    redis = AsyncMock()
    cleanup = DeadStreamCleanup(registry, redis)

    await cleanup.cleanup()

    registry.delete_stream.assert_awaited_once_with(
        "stream-1",
        expected_status="stopped",
        expected_updated_at="2026-05-05T10:00:00Z",
    )
    redis.delete.assert_not_awaited()
