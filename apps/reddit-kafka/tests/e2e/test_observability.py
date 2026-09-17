import json
import os
import time
import uuid
from urllib.request import Request, urlopen

import pytest
from confluent_kafka import Consumer

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("RUN_E2E") != "1",
        reason="run through scripts/run-e2e.sh",
    ),
]

APP_A = f"http://127.0.0.1:{os.getenv('E2E_APP_A_PORT', '18001')}"
KAFKA_ADDRESS = f"127.0.0.1:{os.getenv('E2E_KAFKA_PORT', '19092')}"
KAFKA_TOPIC = "reddit_e2e_comments"


def test_readiness_metrics_and_kafka_trace_propagation() -> None:
    with urlopen(f"{APP_A}/ready", timeout=5) as response:
        readiness = json.loads(response.read())

    assert readiness == {
        "status": "ready",
        "checks": {
            "postgres": True,
            "redis": True,
            "kafka": True,
            "reddit": True,
        },
    }

    request = Request(
        f"{APP_A}/streams?subreddit=e2e_observability",
        method="POST",
        headers={"x-request-id": "e2e-correlation-id"},
    )
    with urlopen(request, timeout=5) as response:
        created = json.loads(response.read())
        assert response.headers["x-request-id"] == "e2e-correlation-id"

    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_ADDRESS,
            "group.id": f"reddit-kafka-observability-{uuid.uuid4()}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([KAFKA_TOPIC])

    try:
        deadline = time.monotonic() + 20
        traceparent: bytes | None = None
        while time.monotonic() < deadline:
            message = consumer.poll(1)
            if message is None:
                continue
            assert message.error() is None, str(message.error())
            payload = json.loads(message.value())
            if payload["subreddit"] != "e2e_observability":
                continue
            headers = dict(message.headers() or [])
            traceparent = headers.get("traceparent")
            break

        assert traceparent is not None
        assert traceparent.decode().startswith("00-")
    finally:
        consumer.close()
        stop = Request(
            f"{APP_A}/streams/{created['id']}/stop",
            method="POST",
        )
        with urlopen(stop, timeout=5):
            pass

    with urlopen(f"{APP_A}/metrics", timeout=5) as response:
        metrics = response.read().decode()

    assert "reddit_kafka_http_requests_total" in metrics
    assert 'route="/streams"' in metrics
    assert "reddit_kafka_reddit_comments_received_total" in metrics
    assert 'reddit_kafka_comments_total{outcome="queued"}' in metrics
    assert 'reddit_kafka_kafka_delivery_bytes_total{result="success"}' in metrics
    assert 'stream_id="' not in metrics
    assert 'subreddit="' not in metrics
    assert "e2e_observability" not in metrics
