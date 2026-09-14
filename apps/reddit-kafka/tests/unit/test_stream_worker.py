import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.stream.worker as worker_module
from src.stream.circuit_breaker import CircuitBreaker
from src.stream.worker import LockLostError, StreamWorker


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


@pytest.mark.asyncio
async def test_checkpoint_flush_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment = SimpleNamespace(
        id="comment-1",
        author=SimpleNamespace(name="author-1"),
        body="message",
    )
    kafka_producer = MagicMock()
    to_thread = AsyncMock(
        side_effect=lambda function, *args, **kwargs: function(*args, **kwargs)
    )

    worker = StreamWorker.__new__(StreamWorker)
    worker.subreddit = "python"
    worker.stream_id = "stream-1"
    worker.kafka_topic = "raw-text"
    worker.kafka_producer = kafka_producer
    worker.serializer = MagicMock()
    worker.serializer.serialize.return_value = b"serialized-message"
    worker.checkpoint_interval = 100
    worker.comments_since_checkpoint = 99
    worker._save_checkpoint_for_comment = AsyncMock()
    worker.error_handler = SimpleNamespace(record_error=AsyncMock())
    monkeypatch.setattr(worker_module.asyncio, "to_thread", to_thread)

    await worker._process_comment(comment, {})

    worker._save_checkpoint_for_comment.assert_awaited_once_with(comment)
    to_thread.assert_awaited_once_with(kafka_producer.flush)
    kafka_producer.flush.assert_called_once_with()
    assert worker.comments_since_checkpoint == 0


def make_lock_test_worker() -> StreamWorker:
    worker = StreamWorker.__new__(StreamWorker)
    worker.subreddit = "python"
    worker.stream_id = "stream-1"
    worker.instance_id = "process-1"
    worker.lock_token = "process-1:current-lease"
    worker.lock_refresh_interval = 0
    worker.lock_ttl = 60
    worker.lock_refresh_timeout = 1
    worker._stop_event = asyncio.Event()
    worker.registry = SimpleNamespace(
        update_status=AsyncMock(),
    )
    worker._save_checkpoint = AsyncMock()
    return worker


@pytest.mark.asyncio
async def test_lock_loss_cancels_idle_stream_and_propagates_error() -> None:
    worker = make_lock_test_worker()
    stream_started = asyncio.Event()
    stream_cancelled = asyncio.Event()

    async def idle_stream() -> None:
        stream_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stream_cancelled.set()
            raise

    async def lose_lock(*args, **kwargs) -> bool:
        del args, kwargs
        await stream_started.wait()
        return False

    worker._stream_loop = idle_stream
    worker.lock_manager = SimpleNamespace(
        refresh_lock=AsyncMock(side_effect=lose_lock),
        release_lock=AsyncMock(return_value=False),
    )

    with pytest.raises(LockLostError, match="Lock ownership lost"):
        await asyncio.wait_for(worker.run(), timeout=1)

    assert stream_cancelled.is_set()
    assert worker._stop_event.is_set()
    worker.lock_manager.release_lock.assert_awaited_once_with(
        "python", "process-1:current-lease"
    )


@pytest.mark.asyncio
async def test_refresh_error_cancels_idle_stream_and_propagates_error() -> None:
    worker = make_lock_test_worker()
    stream_started = asyncio.Event()
    stream_cancelled = asyncio.Event()

    async def idle_stream() -> None:
        stream_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stream_cancelled.set()
            raise

    async def fail_refresh(*args, **kwargs) -> bool:
        del args, kwargs
        await stream_started.wait()
        raise ConnectionError("Redis unavailable")

    worker._stream_loop = idle_stream
    worker.lock_manager = SimpleNamespace(
        refresh_lock=AsyncMock(side_effect=fail_refresh),
        release_lock=AsyncMock(return_value=False),
    )

    with pytest.raises(LockLostError, match="Could not verify lock ownership"):
        await asyncio.wait_for(worker.run(), timeout=1)

    assert stream_cancelled.is_set()
    assert worker._stop_event.is_set()
