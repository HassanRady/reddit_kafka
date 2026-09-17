"""Application observability: metrics, traces, and structured logging."""

from src.observability.metrics import METRICS
from src.observability.telemetry import (
    configure_observability,
    get_tracer,
    instrument_sqlalchemy,
    kafka_trace_headers,
    observe_http_request,
    shutdown_observability,
)

__all__ = [
    "METRICS",
    "configure_observability",
    "get_tracer",
    "instrument_sqlalchemy",
    "kafka_trace_headers",
    "observe_http_request",
    "shutdown_observability",
]
