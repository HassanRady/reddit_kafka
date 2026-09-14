import os

import pytest
import redis.asyncio as redis

from src.repositories.stream_registry import StreamRegistry
from src.tasks.dead_stream_cleanup import DeadStreamCleanup

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("RUN_E2E") != "1",
        reason="run through scripts/run-e2e.sh",
    ),
]


@pytest.mark.asyncio
async def test_cleanup_preserves_restart_checkpoint_and_successor_lock() -> None:
    client = redis.Redis(
        host="127.0.0.1",
        port=int(os.getenv("E2E_REDIS_PORT", "16379")),
        username="local",
        password="password",
        decode_responses=True,
    )
    registry = StreamRegistry(client)
    cleanup = DeadStreamCleanup(registry, client)
    subreddit = "e2e_cleanup_safety"
    successor_token = "process-2:successor-lease"
    stream_id: str | None = None

    try:
        created = await registry.create_stream(subreddit, instance_id="process-1")
        stream_id = created["id"]
        await registry.set_checkpoint(
            stream_id,
            last_comment_id="comment-before-stop",
            last_processed_at="2026-05-05T10:00:00Z",
        )
        await registry.update_status(stream_id, "stopped", instance_id="process-1")
        stopped_snapshot = await registry.get_stream(stream_id)
        await client.set(f"stream:lock:{subreddit}", successor_token, ex=60)

        await cleanup.cleanup()

        assert not await client.exists(f"stream:meta:{stream_id}")
        assert (
            await client.hget(f"stream:checkpoint:{stream_id}", "last_comment_id")
            == "comment-before-stop"
        )
        assert await client.get(f"stream:lock:{subreddit}") == successor_token

        restarted = await registry.create_stream(subreddit, instance_id="process-2")

        assert restarted["id"] == stream_id
        checkpoint = await registry.get_checkpoint(stream_id)
        assert checkpoint["last_comment_id"] == "comment-before-stop"

        stale_delete = await registry.delete_stream(
            stream_id,
            expected_status=str(stopped_snapshot["status"]),
            expected_updated_at=str(stopped_snapshot["updated_at"]),
        )
        assert stale_delete is False
        assert (await registry.get_stream(stream_id))["instance_id"] == "process-2"
        assert await client.get(f"stream:lock:{subreddit}") == successor_token
    finally:
        keys = [
            f"stream:subreddit:{subreddit}",
            f"stream:identity:{subreddit}",
            f"stream:lock:{subreddit}",
        ]
        if stream_id is not None:
            keys.extend(
                [
                    f"stream:meta:{stream_id}",
                    f"stream:checkpoint:{stream_id}",
                    f"stream:stop-request:{stream_id}",
                ]
            )
        await client.delete(*keys)
        if stream_id is not None:
            await client.srem("streams:all", stream_id)
        await client.aclose()
