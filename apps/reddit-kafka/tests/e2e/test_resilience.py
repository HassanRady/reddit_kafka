import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import asyncpg
import pytest
import redis
from confluent_kafka import Consumer

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
        except (AssertionError, KeyError, StopIteration, TypeError) as error:
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


def api_response(base_url: str, method: str, path: str) -> tuple[int, Any]:
    request = Request(f"{base_url}{path}", method=method)
    try:
        with urlopen(request, timeout=5) as response:
            status = response.status
            body = response.read()
    except HTTPError as error:
        status = error.code
        body = error.read()
    return status, json.loads(body) if body else None


def api_request(
    base_url: str,
    method: str,
    path: str,
    *,
    expected_status: int = 200,
) -> Any:
    status, body = api_response(base_url, method, path)
    assert status == expected_status, body
    return body


def stream_from_api(base_url: str, stream_id: str) -> dict[str, Any]:
    streams = api_request(base_url, "GET", "/streams")
    return next(stream for stream in streams if stream["id"] == stream_id)


def require_stream_status(base_url: str, stream_id: str, status: str) -> dict[str, Any]:
    stream = stream_from_api(base_url, stream_id)
    assert stream["status"] == status
    return stream


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


def kafka_consumer() -> Consumer:
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_ADDRESS,
            "group.id": f"reddit-kafka-resilience-{time.monotonic_ns()}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([KAFKA_TOPIC])
    return consumer


def kafka_record_for(
    consumer: Consumer,
    subreddit: str,
    *,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    timeout: float = 20,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = consumer.poll(1)
        if message is None:
            continue
        assert message.error() is None, str(message.error())
        record = json.loads(message.value().decode("utf-8"))
        if record.get("subreddit") == subreddit and (
            predicate is None or predicate(record)
        ):
            return record
    raise AssertionError(f"no matching Kafka record received for {subreddit}")


def assert_redis_error(
    client: redis.Redis, stream_id: str, error_type: str
) -> dict[str, Any]:
    entries = client.lrange(f"stream:errors:{stream_id}", 0, -1)
    parsed = [json.loads(entry) for entry in entries]
    match = next((entry for entry in parsed if entry["error_type"] == error_type), None)
    assert match is not None, parsed
    return match


def stop_and_wait(base_url: str, stream_id: str) -> None:
    api_request(base_url, "POST", f"/streams/{stream_id}/stop")
    eventually(lambda: require_stream_status(base_url, stream_id, "stopped"))


def test_api_validation_errors_do_not_create_registry_state() -> None:
    cases = [
        ("/streams", 422),
        ("/streams?subreddit=", 422),
        (f"/streams?subreddit={'x' * 256}", 422),
        ("/streams?subreddit=e2e_missing", 404),
        ("/streams?subreddit=e2e_forbidden", 403),
        ("/streams?subreddit=e2e_validation_outage", 503),
    ]

    for path, expected_status in cases:
        status, body = api_response(APP_A, "POST", path)
        assert status == expected_status, body

    rejected_names = {
        "",
        "x" * 256,
        "e2e_missing",
        "e2e_forbidden",
        "e2e_validation_outage",
    }
    streams = api_request(APP_B, "GET", "/streams")
    assert rejected_names.isdisjoint(stream["subreddit"] for stream in streams)


@pytest.mark.asyncio
async def test_concurrent_creation_has_one_global_winner() -> None:
    subreddit = "e2e_concurrent_create"
    path = f"/streams?subreddit={quote(subreddit)}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda base_url: api_response(base_url, "POST", path),
                (APP_A, APP_B),
            )
        )

    assert sorted(status for status, _ in responses) == [200, 409]
    created = next(body for status, body in responses if status == 200)
    conflict = next(body for status, body in responses if status == 409)
    assert "already exists" in conflict["detail"]
    stream_id = created["id"]

    client = redis.Redis(
        host="127.0.0.1",
        port=int(os.getenv("E2E_REDIS_PORT", "16379")),
        username="local",
        password="password",
        decode_responses=True,
    )
    try:
        assert client.get(f"stream:subreddit:{subreddit}") == stream_id
        assert client.scard("streams:all") >= 1
        row = await eventually_async(
            lambda: required_database_row(
                "SELECT COUNT(*) AS count FROM streams WHERE subreddit = $1",
                subreddit,
            )
        )
        assert row["count"] == 1
        stop_and_wait(APP_B, stream_id)
    finally:
        client.close()


@pytest.mark.asyncio
async def test_comment_edge_cases_are_isolated_and_errors_are_durable() -> None:
    subreddit = "e2e_comment_edges"
    consumer = kafka_consumer()
    client = redis.Redis(
        host="127.0.0.1",
        port=int(os.getenv("E2E_REDIS_PORT", "16379")),
        username="local",
        password="password",
        decode_responses=True,
    )
    try:
        created = api_request(APP_A, "POST", f"/streams?subreddit={quote(subreddit)}")
        stream_id = created["id"]

        deleted_author = kafka_record_for(
            consumer,
            subreddit,
            predicate=lambda record: record["author_id"] == "[deleted]",
        )
        assert deleted_author["text"] == "deterministic E2E comment 1"

        following_record = kafka_record_for(
            consumer,
            subreddit,
            predicate=lambda record: record["text"].endswith("3"),
        )
        assert following_record["author_id"] == "e2e-author"

        error = eventually(
            lambda: assert_redis_error(client, stream_id, "CommentProcessingError")
        )
        assert error["is_recoverable"] == "1"
        durable_error = await eventually_async(
            lambda: required_database_row(
                """
                SELECT error_type, is_recoverable
                FROM stream_errors
                WHERE stream_id = $1 AND error_type = 'CommentProcessingError'
                """,
                stream_id,
            )
        )
        assert durable_error["is_recoverable"] == 1
        require_stream_status(APP_B, stream_id, "active")
        stop_and_wait(APP_B, stream_id)
    finally:
        consumer.close()
        client.close()


@pytest.mark.asyncio
async def test_transient_failure_recovers_and_fatal_failure_releases_lock() -> None:
    consumer = kafka_consumer()
    client = redis.Redis(
        host="127.0.0.1",
        port=int(os.getenv("E2E_REDIS_PORT", "16379")),
        username="local",
        password="password",
        decode_responses=True,
    )
    try:
        transient = api_request(
            APP_A, "POST", "/streams?subreddit=e2e_transient_failure"
        )
        transient_id = transient["id"]
        assert kafka_record_for(consumer, "e2e_transient_failure")
        transient_error = eventually(
            lambda: assert_redis_error(client, transient_id, "RequestException")
        )
        assert transient_error["is_recoverable"] == "1"
        require_stream_status(APP_B, transient_id, "active")
        stop_and_wait(APP_B, transient_id)

        fatal = api_request(APP_B, "POST", "/streams?subreddit=e2e_fatal_failure")
        fatal_id = fatal["id"]
        eventually(lambda: require_stream_status(APP_A, fatal_id, "error"))
        eventually(
            lambda: (
                True
                if client.exists("stream:lock:e2e_fatal_failure") == 0
                else (_ for _ in ()).throw(
                    AssertionError("fatal lock was not released")
                )
            )
        )
        fatal_error = eventually(
            lambda: assert_redis_error(client, fatal_id, "ValueError")
        )
        assert fatal_error["is_recoverable"] == "0"
        durable_error = await eventually_async(
            lambda: required_database_row(
                """
                SELECT error_type, is_recoverable
                FROM stream_errors
                WHERE stream_id = $1 AND error_type = 'ValueError'
                """,
                fatal_id,
            )
        )
        assert durable_error["is_recoverable"] == 0
        assert api_request(APP_A, "POST", f"/streams/{fatal_id}/stop") == {
            "stream_id": fatal_id,
            "status": "error",
        }
    finally:
        consumer.close()
        client.close()


@pytest.mark.asyncio
async def test_lock_loss_fails_closed_then_stream_is_adopted() -> None:
    subreddit = "e2e_lock_loss"
    successor_token = "successor-instance:replacement-lease"
    client = redis.Redis(
        host="127.0.0.1",
        port=int(os.getenv("E2E_REDIS_PORT", "16379")),
        username="local",
        password="password",
        decode_responses=True,
    )
    try:
        created = api_request(APP_A, "POST", f"/streams?subreddit={subreddit}")
        stream_id = created["id"]
        original = eventually(lambda: require_stream_status(APP_B, stream_id, "active"))
        original_instance = original["instance_id"]

        lock_key = f"stream:lock:{subreddit}"
        original_token = client.get(lock_key)
        assert original_token and original_token != successor_token
        client.set(lock_key, successor_token, ex=60)

        # The old worker must fail closed without deleting a lease that may
        # belong to a successor. Lease loss is not a terminal stream error.
        time.sleep(1)
        assert require_stream_status(APP_B, stream_id, "active")["instance_id"] == (
            original_instance
        )
        assert client.get(lock_key) == successor_token

        # Once the foreign lease disappears, exactly one reconciler adopts the
        # stream and records its new owner.
        client.delete(lock_key)
        replacement_token = eventually(
            lambda: (
                client.get(lock_key)
                if client.get(lock_key) not in {None, original_token, successor_token}
                else (_ for _ in ()).throw(
                    AssertionError("stream has not been adopted")
                )
            ),
            timeout=10,
        )
        assert replacement_token not in {original_token, successor_token}
        adopted = require_stream_status(APP_B, stream_id, "active")

        row = await eventually_async(
            lambda: required_database_row(
                "SELECT status, instance_id FROM streams WHERE id = $1", stream_id
            )
        )
        assert row["status"] == "active"
        assert row["instance_id"] == adopted["instance_id"]
        stop_and_wait(APP_A, stream_id)
    finally:
        lock_key = f"stream:lock:{subreddit}"
        if client.get(lock_key) == successor_token:
            client.delete(lock_key)
        client.close()
