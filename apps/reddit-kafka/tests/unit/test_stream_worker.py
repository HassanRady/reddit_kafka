import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import CollectorRegistry

import src.stream.worker as worker_module
from src.observability.metrics import ApplicationMetrics
from src.stream.circuit_breaker import CircuitBreaker
from src.stream.exceptions import KafkaDeliveryError
from src.stream.worker import LockLostError, StreamWorker


@pytest.mark.asyncio
async def test_received_comment_resets_failures_while_stream_remains_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    received_counter = MagicMock()
    monkeypatch.setattr(
        worker_module,
        "METRICS",
        SimpleNamespace(reddit_comments_received=received_counter),
    )

    stream_task = asyncio.create_task(worker._fetch_and_process_comments())
    try:
        await asyncio.wait_for(processed.wait(), timeout=1)

        assert not stream_task.done()
        assert worker.circuit_breaker.fail_count == 0
        received_counter.inc.assert_called_once_with()
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
    kafka_producer.flush.return_value = 0
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
    worker._delivery_errors = []
    worker._save_checkpoint_for_comment_id = AsyncMock()
    worker.error_handler = SimpleNamespace(record_error=AsyncMock())
    monkeypatch.setattr(worker_module.asyncio, "to_thread", to_thread)

    await worker._process_comment(comment, {})

    worker._save_checkpoint_for_comment_id.assert_awaited_once_with("comment-1")
    to_thread.assert_awaited_once_with(kafka_producer.flush)
    kafka_producer.flush.assert_called_once_with()
    kafka_producer.produce.assert_called_once_with(
        "raw-text",
        b"serialized-message",
        key=b"python",
        on_delivery=worker._on_delivery,
    )
    assert worker.comments_since_checkpoint == 0


def test_delivery_callback_records_successful_payload_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = ApplicationMetrics(CollectorRegistry())
    monkeypatch.setattr(worker_module, "METRICS", metrics)
    worker = StreamWorker.__new__(StreamWorker)
    worker._delivery_errors = []
    message = SimpleNamespace(value=lambda: b"serialized-message")

    worker._on_delivery(None, message)

    payload = metrics.render().decode()
    assert 'reddit_kafka_kafka_deliveries_total{result="success"} 1.0' in payload
    assert 'reddit_kafka_kafka_delivery_bytes_total{result="success"} 18.0' in payload


@pytest.mark.asyncio
async def test_delivery_failure_does_not_advance_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment = SimpleNamespace(
        id="comment-1",
        author=SimpleNamespace(name="author-1"),
        body="message",
    )
    kafka_producer = MagicMock()

    def flush() -> int:
        delivery_callback = kafka_producer.produce.call_args.kwargs["on_delivery"]
        delivery_callback(RuntimeError("broker rejected message"), MagicMock())
        return 0

    to_thread = AsyncMock(side_effect=lambda function: function())
    kafka_producer.flush.side_effect = flush

    worker = StreamWorker.__new__(StreamWorker)
    worker.subreddit = "python"
    worker.stream_id = "stream-1"
    worker.kafka_topic = "raw-text"
    worker.kafka_producer = kafka_producer
    worker.serializer = MagicMock()
    worker.serializer.serialize.return_value = b"serialized-message"
    worker.checkpoint_interval = 100
    worker.comments_since_checkpoint = 99
    worker._delivery_errors = []
    worker._save_checkpoint_for_comment_id = AsyncMock()
    worker.error_handler = SimpleNamespace(record_error=AsyncMock())
    monkeypatch.setattr(worker_module.asyncio, "to_thread", to_thread)

    with pytest.raises(KafkaDeliveryError, match="broker rejected message"):
        await worker._process_comment(comment, {})

    worker._save_checkpoint_for_comment_id.assert_not_awaited()
    assert worker.comments_since_checkpoint == 100


@pytest.mark.asyncio
async def test_shutdown_checkpoint_uses_stored_comment_id() -> None:
    worker = StreamWorker.__new__(StreamWorker)
    worker.stream_id = "stream-1"
    worker.registry = SimpleNamespace(
        get_checkpoint=AsyncMock(return_value={"last_comment_id": "comment-42"})
    )
    worker._save_checkpoint_for_comment_id = AsyncMock()

    await worker._save_checkpoint()

    worker._save_checkpoint_for_comment_id.assert_awaited_once_with("comment-42")


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
        heartbeat=AsyncMock(),
    )
    worker._save_checkpoint = AsyncMock()
    return worker


@pytest.mark.asyncio
async def test_lock_refresh_records_redis_heartbeat() -> None:
    worker = make_lock_test_worker()
    refresh_count = 0

    async def refresh_lock(*args, **kwargs) -> bool:
        nonlocal refresh_count
        del args, kwargs
        refresh_count += 1
        if refresh_count == 1:
            return True
        raise asyncio.CancelledError

    worker.lock_manager = SimpleNamespace(refresh_lock=refresh_lock)

    with pytest.raises(asyncio.CancelledError):
        await worker._lock_refresh_loop()

    worker.registry.heartbeat.assert_awaited_once_with("stream-1", "process-1")


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
