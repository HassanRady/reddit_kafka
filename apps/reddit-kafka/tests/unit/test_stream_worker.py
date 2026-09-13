import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.stream.circuit_breaker import CircuitBreaker
from src.stream.worker import StreamWorker


@pytest.mark.asyncio
async def test_received_comment_resets_failures_while_stream_remains_open() -> None:
    comment = SimpleNamespace(id="comment-1")
    keep_stream_open = asyncio.Event()

    async def comments(*, skip_existing: bool):
        assert skip_existing is True
        yield comment
        await keep_stream_open.wait()

    subreddit = SimpleNamespace(
        stream=SimpleNamespace(comments=comments),
    )
    processed = asyncio.Event()

    async def process_comment(received_comment, checkpoint):
        assert received_comment is comment
        assert checkpoint == {}
        processed.set()

    worker = StreamWorker.__new__(StreamWorker)
    worker.subreddit = "python"
    worker.stream_id = "stream-1"
    worker.reddit_client = SimpleNamespace(
        subreddit=AsyncMock(return_value=subreddit),
    )
    worker.registry = SimpleNamespace(
        get_checkpoint=AsyncMock(return_value={}),
    )
    worker._stop_event = asyncio.Event()
    worker._process_comment = process_comment
    worker.circuit_breaker = CircuitBreaker(failure_threshold=5)
    worker.circuit_breaker.failure_count = 4

    stream_task = asyncio.create_task(worker._fetch_and_process_comments())
    try:
        await asyncio.wait_for(processed.wait(), timeout=1)

        assert not stream_task.done()
        assert worker.circuit_breaker.fail_count == 0
    finally:
        stream_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stream_task
