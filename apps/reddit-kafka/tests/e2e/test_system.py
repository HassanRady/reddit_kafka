import asyncio
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import asyncpg
import pytest
import redis
from confluent_kafka import Consumer, TopicPartition

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("RUN_E2E") != "1",
        reason="run through scripts/run-e2e.sh",
    ),
]

APP_A = f"http://127.0.0.1:{os.getenv('E2E_APP_A_PORT', '18001')}"
APP_B = f"http://127.0.0.1:{os.getenv('E2E_APP_B_PORT', '18002')}"
KAFKA_ADDRESS = f"127.0.0.1:{os.getenv('E2E_KAFKA_PORT', '19092')}"
KAFKA_TOPIC = "reddit_e2e_comments"


def eventually(
    assertion: Callable[[], Any], *, timeout: float = 20, interval: float = 0.1
) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return assertion()
        except (AssertionError, KeyError, TypeError) as error:
            last_error = error
            time.sleep(interval)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition was not satisfied")


async def eventually_async(
    assertion: Callable[[], Awaitable[Any]],
    *,
    timeout: float = 20,
    interval: float = 0.1,
) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return await assertion()
        except (AssertionError, KeyError, TypeError) as error:
            last_error = error
            await asyncio.sleep(interval)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition was not satisfied")


def api_request(
    base_url: str,
    method: str,
    path: str,
    *,
    expected_status: int = 200,
) -> Any:
    request = Request(f"{base_url}{path}", method=method)
    try:
        with urlopen(request, timeout=5) as response:
            status = response.status
            body = response.read()
    except HTTPError as error:
        status = error.code
        body = error.read()

    assert status == expected_status, body.decode("utf-8", errors="replace")
    return json.loads(body) if body else None


def stream_from_api(base_url: str, stream_id: str) -> dict[str, Any]:
    streams = api_request(base_url, "GET", "/streams")
    return next(stream for stream in streams if stream["id"] == stream_id)


async def database_row(query: str, *args: Any) -> asyncpg.Record | None:
    connection = await asyncpg.connect(
        host="127.0.0.1",
        port=int(os.getenv("E2E_POSTGRES_PORT", "15432")),
        user="local",
        password="password",
        database="reddit_stream_e2e",
    )
    try:
        return await connection.fetchrow(query, *args)
    finally:
        await connection.close()


async def required_database_row(query: str, *args: Any) -> asyncpg.Record:
    row = await database_row(query, *args)
    assert row is not None, "database row was not persisted"
    return row


def kafka_record(consumer: Consumer, subreddit: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        message = consumer.poll(1)
        if message is None:
            continue
        assert message.error() is None, str(message.error())
        record = json.loads(message.value().decode("utf-8"))
        if record.get("subreddit") == subreddit:
            return record
    raise AssertionError(f"no Kafka record received for {subreddit}")


def kafka_high_watermark(consumer: Consumer) -> int:
    metadata = consumer.list_topics(KAFKA_TOPIC, timeout=10)
    topic = metadata.topics[KAFKA_TOPIC]
    assert topic.error is None, str(topic.error)
    return sum(
        consumer.get_watermark_offsets(
            TopicPartition(KAFKA_TOPIC, partition_id), timeout=5, cached=False
        )[1]
        for partition_id in topic.partitions
    )


@pytest.mark.asyncio
async def test_complete_stream_lifecycle_across_two_app_instances() -> None:
    assert api_request(APP_A, "GET", "/health") == {"status": "ok"}
    assert api_request(APP_B, "GET", "/health") == {"status": "ok"}
    api_request(APP_B, "POST", "/streams/missing/stop", expected_status=404)

    redis_client = redis.Redis(
        host="127.0.0.1",
        port=int(os.getenv("E2E_REDIS_PORT", "16379")),
        username="local",
        password="password",
        decode_responses=True,
    )
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_ADDRESS,
            "group.id": f"reddit-kafka-e2e-{uuid.uuid4()}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([KAFKA_TOPIC])

    try:
        created = api_request(
            APP_A,
            "POST",
            f"/streams?subreddit={quote('e2e_python')}",
        )
        stream_id = created["id"]
        assert created["subreddit"] == "e2e_python"

        duplicate = api_request(
            APP_B,
            "POST",
            f"/streams?subreddit={quote('e2e_python')}",
            expected_status=409,
        )
        assert "already exists" in duplicate["detail"]

        active = eventually(
            lambda: (
                stream_from_api(APP_B, stream_id)
                if stream_from_api(APP_B, stream_id)["status"] == "active"
                else (_ for _ in ()).throw(AssertionError("stream is not active"))
            )
        )
        assert active["instance_id"]
        assert redis_client.exists("stream:lock:e2e_python") == 1

        record = kafka_record(consumer, "e2e_python")
        assert record["subreddit"] == "e2e_python"
        assert record["author_id"] == "e2e-author"
        assert record["text"].startswith("deterministic E2E comment")
        assert record["timestamp"].endswith("Z")

        checkpoint = eventually(
            lambda: (
                redis_client.hgetall(f"stream:checkpoint:{stream_id}")
                or (_ for _ in ()).throw(AssertionError("checkpoint not written"))
            ),
            timeout=30,
        )
        assert checkpoint["last_comment_id"].startswith("e2e-comment-")

        persisted_checkpoint = await eventually_async(
            lambda: required_database_row(
                "SELECT last_comment_id FROM stream_checkpoints WHERE stream_id = $1",
                stream_id,
            ),
            timeout=30,
        )
        assert persisted_checkpoint["last_comment_id"].startswith("e2e-comment-")

        # The stop intentionally goes to app-b; app-a owns and must cancel the task.
        stopping = api_request(APP_B, "POST", f"/streams/{stream_id}/stop")
        assert stopping == {"stream_id": stream_id, "status": "stopping"}

        stopped = eventually(
            lambda: (
                stream_from_api(APP_B, stream_id)
                if stream_from_api(APP_B, stream_id)["status"] == "stopped"
                else (_ for _ in ()).throw(AssertionError("stream is not stopped"))
            ),
            timeout=10,
        )
        assert stopped["status"] == "stopped"
        eventually(
            lambda: (
                True
                if redis_client.exists("stream:lock:e2e_python") == 0
                else (_ for _ in ()).throw(AssertionError("stream lock still exists"))
            ),
            timeout=10,
        )

        database_stream = await eventually_async(
            lambda: required_database_row(
                "SELECT status FROM streams WHERE id = $1", stream_id
            )
        )
        assert database_stream["status"] == "stopped"

        # Stopping a terminal stream is idempotent and reports its real state.
        assert api_request(APP_A, "POST", f"/streams/{stream_id}/stop") == {
            "stream_id": stream_id,
            "status": "stopped",
        }

        time.sleep(0.5)
        watermark_after_stop = kafka_high_watermark(consumer)
        time.sleep(1.5)
        assert kafka_high_watermark(consumer) == watermark_after_stop
    finally:
        consumer.close()
        redis_client.close()
