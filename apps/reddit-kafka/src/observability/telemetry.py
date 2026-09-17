"""OpenTelemetry setup, trace propagation, and trace-correlated JSON logs."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import Request, Response
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from sqlalchemy.ext.asyncio import AsyncEngine

from src.config import ObservabilitySettings
from src.observability.metrics import METRICS

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)
_runtime: ObservabilityRuntime | None = None
_sqlalchemy_engines: set[int] = set()
_traces_enabled = False


class JsonLogFormatter(logging.Formatter):
    """Emit one machine-readable event while preserving trace correlation."""

    def __init__(self, settings: ObservabilitySettings, instance_id: str) -> None:
        super().__init__()
        self.service_name = settings.service_name
        self.environment = settings.environment
        self.instance_id = instance_id

    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": record.levelname,
            "service": self.service_name,
            "environment": self.environment,
            "instance_id": self.instance_id,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = _request_id.get()
        if request_id:
            payload["request_id"] = request_id
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")

        for attribute in (
            "event",
            "stream_id",
            "subreddit",
            "error_type",
            "recoverable",
            "duration_ms",
            "http_method",
            "http_route",
            "status_code",
        ):
            value = getattr(record, attribute, None)
            if value is not None:
                payload[attribute] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


class _DropTelemetryInternals(logging.Filter):
    """Prevent exporter diagnostics from recursively exporting themselves."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(("opentelemetry", "grpc"))


class ObservabilityRuntime:
    def __init__(
        self,
        tracer_provider: TracerProvider,
        logger_provider: LoggerProvider | None,
        otel_log_handler: LoggingHandler | None,
    ) -> None:
        self.tracer_provider = tracer_provider
        self.logger_provider = logger_provider
        self.otel_log_handler = otel_log_handler


def _configure_console_logging(
    settings: ObservabilitySettings, instance_id: str
) -> None:
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())

    for handler in list(root.handlers):
        if getattr(handler, "_reddit_kafka_console", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler._reddit_kafka_console = True  # type: ignore[attr-defined]
    if settings.json_logs:
        handler.setFormatter(JsonLogFormatter(settings, instance_id))
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s "
                "[instance=%(instance_id)s] %(message)s",
                defaults={"instance_id": instance_id},
            )
        )
    root.addHandler(handler)


def configure_observability(
    settings: ObservabilitySettings, instance_id: str
) -> ObservabilityRuntime:
    """Configure telemetry once; an unavailable collector never blocks startup."""
    global _runtime, _traces_enabled

    _configure_console_logging(settings, instance_id)
    if _runtime is not None:
        return _runtime

    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.service_version,
            "service.instance.id": instance_id,
            "deployment.environment.name": settings.environment,
        }
    )
    _traces_enabled = settings.traces_enabled
    sampling_ratio = settings.trace_sample_ratio if settings.traces_enabled else 0.0
    tracer_provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(sampling_ratio)),
    )
    trace.set_tracer_provider(tracer_provider)

    logger_provider: LoggerProvider | None = None
    otel_log_handler: LoggingHandler | None = None
    endpoint = settings.otlp_endpoint
    if endpoint and settings.traces_enabled:
        span_exporter = OTLPSpanExporter(
            endpoint=endpoint,
            insecure=endpoint.startswith("http://"),
        )
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))

    if endpoint and settings.logs_export_enabled:
        logger_provider = LoggerProvider(resource=resource)
        log_exporter = OTLPLogExporter(
            endpoint=endpoint,
            insecure=endpoint.startswith("http://"),
        )
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        otel_log_handler = LoggingHandler(
            level=logging.NOTSET, logger_provider=logger_provider
        )
        otel_log_handler.addFilter(_DropTelemetryInternals())
        logging.getLogger().addHandler(otel_log_handler)

    if settings.traces_enabled:
        AioHttpClientInstrumentor().instrument(tracer_provider=tracer_provider)
        BotocoreInstrumentor().instrument(  # type: ignore[no-untyped-call]
            tracer_provider=tracer_provider
        )
        RedisInstrumentor().instrument(tracer_provider=tracer_provider)

    _runtime = ObservabilityRuntime(
        tracer_provider=tracer_provider,
        logger_provider=logger_provider,
        otel_log_handler=otel_log_handler,
    )
    return _runtime


async def shutdown_observability() -> None:
    """Drain background exporters without blocking the event loop."""
    global _runtime, _traces_enabled

    runtime = _runtime
    if runtime is None:
        return

    if runtime.otel_log_handler is not None:
        logging.getLogger().removeHandler(runtime.otel_log_handler)
    if runtime.logger_provider is not None:
        await asyncio.to_thread(runtime.logger_provider.shutdown)
    await asyncio.to_thread(runtime.tracer_provider.shutdown)
    _traces_enabled = False
    _runtime = None


def instrument_sqlalchemy(engine: AsyncEngine) -> None:
    """Attach SQL spans once for each async engine."""
    if not _traces_enabled:
        return
    engine_id = id(engine)
    if engine_id in _sqlalchemy_engines:
        return
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    _sqlalchemy_engines.add(engine_id)


def get_tracer(component: str) -> Tracer:
    return trace.get_tracer(f"reddit_kafka.{component}")


def kafka_trace_headers() -> list[tuple[str, bytes]]:
    """Create Kafka headers using the configured W3C propagator."""
    if not _traces_enabled:
        return []
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return [(key, value.encode("utf-8")) for key, value in carrier.items()]


def _request_route(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


async def observe_http_request(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Trace requests, expose RED metrics, and return a correlation ID."""
    request_id = request.headers.get("x-request-id", "")
    if not request_id or len(request_id) > 128:
        request_id = str(uuid.uuid4())
    request_token = _request_id.set(request_id)

    if request.url.path in {"/health", "/ready", "/metrics"}:
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            return response
        finally:
            _request_id.reset(request_token)

    method = request.method
    started = time.perf_counter()
    status_code = 500
    METRICS.http_in_progress.labels(method=method).inc()
    parent_context = propagate.extract(dict(request.headers))
    tracer = get_tracer("http")

    try:
        with tracer.start_as_current_span(
            f"HTTP {method}",
            context=parent_context,
            kind=SpanKind.SERVER,
            attributes={
                "http.request.method": method,
                "url.path": request.url.path,
                "server.address": request.url.hostname or "",
            },
        ) as span:
            try:
                response = await call_next(request)
                status_code = response.status_code
            except Exception as error:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                route = _request_route(request)
                duration = time.perf_counter() - started
                span.update_name(f"{method} {route}")
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status_code)
                if status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                logging.getLogger("reddit_kafka.http").info(
                    "HTTP request completed",
                    extra={
                        "event": "http.request.completed",
                        "duration_ms": round(duration * 1000, 3),
                        "http_method": method,
                        "http_route": route,
                        "status_code": status_code,
                    },
                )

            response.headers["x-request-id"] = request_id
            return response
    finally:
        route = _request_route(request)
        duration = time.perf_counter() - started
        METRICS.http_in_progress.labels(method=method).dec()
        METRICS.http_requests.labels(
            method=method,
            route=route,
            status_code=str(status_code),
        ).inc()
        METRICS.http_request_duration.labels(method=method, route=route).observe(
            duration
        )
        _request_id.reset(request_token)


__all__ = [
    "JsonLogFormatter",
    "configure_observability",
    "get_tracer",
    "instrument_sqlalchemy",
    "kafka_trace_headers",
    "observe_http_request",
    "shutdown_observability",
]
