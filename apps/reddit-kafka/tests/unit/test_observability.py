import json
import logging

import pytest
from fastapi import Request, Response
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import CollectorRegistry

import src.observability.telemetry as telemetry_module
from src.config import ObservabilitySettings
from src.observability.metrics import ApplicationMetrics
from src.observability.telemetry import (
    JsonLogFormatter,
    kafka_trace_headers,
    observe_http_request,
)


def test_metrics_use_bounded_operational_labels() -> None:
    metrics = ApplicationMetrics(CollectorRegistry())

    metrics.reddit_comments_received.inc(2)
    metrics.comments.labels(outcome="queued").inc()
    metrics.kafka_delivery_bytes.labels(result="success").inc(512)
    metrics.lock_operations.labels(operation="acquire", result="success").inc()
    payload = metrics.render().decode()

    assert "reddit_kafka_reddit_comments_received_total 2.0" in payload
    assert 'reddit_kafka_comments_total{outcome="queued"} 1.0' in payload
    assert 'reddit_kafka_kafka_delivery_bytes_total{result="success"} 512.0' in payload
    assert (
        'reddit_kafka_lock_operations_total{operation="acquire",result="success"} 1.0'
    ) in payload
    assert "stream_id" not in payload
    assert "subreddit" not in payload


def test_json_logs_include_service_context() -> None:
    settings = ObservabilitySettings(
        OTEL_SERVICE_NAME="test-service",
        DEPLOYMENT_ENVIRONMENT="test",
    )
    formatter = JsonLogFormatter(settings, instance_id="instance-1")
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="stream started",
        args=(),
        exc_info=None,
    )
    record.event = "stream.started"

    payload = json.loads(formatter.format(record))

    assert payload["service"] == "test-service"
    assert payload["environment"] == "test"
    assert payload["instance_id"] == "instance-1"
    assert payload["event"] == "stream.started"
    assert payload["message"] == "stream started"


def test_json_logs_include_current_trace_context() -> None:
    provider = TracerProvider()
    span = provider.get_tracer("test").start_span("operation")
    formatter = JsonLogFormatter(ObservabilitySettings(), instance_id="instance-1")
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="correlated event",
        args=(),
        exc_info=None,
    )

    try:
        with trace.use_span(span):
            payload = json.loads(formatter.format(record))
    finally:
        span.end()
        provider.shutdown()

    assert payload["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert payload["span_id"] == format(span.get_span_context().span_id, "016x")


def test_kafka_headers_propagate_w3c_trace_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = TracerProvider()
    span = provider.get_tracer("test").start_span("producer")
    monkeypatch.setattr(telemetry_module, "_traces_enabled", True)

    try:
        with trace.use_span(span):
            headers = dict(kafka_trace_headers())
    finally:
        span.end()
        provider.shutdown()

    assert headers["traceparent"].decode().startswith("00-")


@pytest.mark.asyncio
async def test_http_observation_preserves_request_id() -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/example/42",
            "raw_path": b"/example/42",
            "query_string": b"",
            "headers": [(b"x-request-id", b"correlation-123")],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
        }
    )

    async def call_next(received_request: Request) -> Response:
        assert received_request is request
        return Response(status_code=200)

    response = await observe_http_request(request, call_next)

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "correlation-123"
