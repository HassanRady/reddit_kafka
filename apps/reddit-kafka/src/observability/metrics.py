"""Low-cardinality Prometheus metrics for the service's golden signals."""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class ApplicationMetrics:
    """Own all application metrics and allow isolated registries in tests."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self.registry = registry

        self.http_requests = Counter(
            "reddit_kafka_http_requests_total",
            "HTTP requests handled by the control plane.",
            ("method", "route", "status_code"),
            registry=registry,
        )
        self.http_request_duration = Histogram(
            "reddit_kafka_http_request_duration_seconds",
            "Control-plane request latency.",
            ("method", "route"),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
            registry=registry,
        )
        self.http_in_progress = Gauge(
            "reddit_kafka_http_requests_in_progress",
            "Control-plane requests currently being handled.",
            ("method",),
            registry=registry,
        )
        self.active_streams = Gauge(
            "reddit_kafka_active_streams",
            "Stream workers currently owned by this process.",
            registry=registry,
        )
        self.stream_lifecycle = Counter(
            "reddit_kafka_stream_lifecycle_total",
            "Stream lifecycle transitions.",
            ("transition", "result"),
            registry=registry,
        )
        self.comments = Counter(
            "reddit_kafka_comments_total",
            "Reddit comments handled by processing outcome.",
            ("outcome",),
            registry=registry,
        )
        self.reddit_comments_received = Counter(
            "reddit_kafka_reddit_comments_received_total",
            "Comments observed on Reddit streams before processing.",
            registry=registry,
        )
        self.comment_processing_duration = Histogram(
            "reddit_kafka_comment_processing_duration_seconds",
            "Time spent validating, serializing, and queuing a comment.",
            ("outcome",),
            buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1),
            registry=registry,
        )
        self.kafka_deliveries = Counter(
            "reddit_kafka_kafka_deliveries_total",
            "Kafka delivery callback outcomes.",
            ("result",),
            registry=registry,
        )
        self.kafka_delivery_bytes = Counter(
            "reddit_kafka_kafka_delivery_bytes_total",
            "Serialized payload bytes reported by Kafka delivery callbacks.",
            ("result",),
            registry=registry,
        )
        # Pre-create bounded children so dashboards render zero before the first
        # delivery callback instead of showing an ambiguous "No data" state.
        for result in ("success", "error"):
            self.kafka_delivery_bytes.labels(result=result)
        self.last_delivery_timestamp = Gauge(
            "reddit_kafka_last_delivery_timestamp_seconds",
            "Unix timestamp of the most recent successful Kafka delivery.",
            registry=registry,
        )
        self.errors = Counter(
            "reddit_kafka_errors_total",
            "Operational errors by bounded exception class and recoverability.",
            ("error_type", "recoverable"),
            registry=registry,
        )
        self.lock_operations = Counter(
            "reddit_kafka_lock_operations_total",
            "Distributed lease operations.",
            ("operation", "result"),
            registry=registry,
        )
        self.checkpoint_flushes = Counter(
            "reddit_kafka_checkpoint_flushes_total",
            "Checkpoint flush attempts.",
            ("result",),
            registry=registry,
        )
        self.checkpoint_batch_size = Histogram(
            "reddit_kafka_checkpoint_batch_size",
            "Number of checkpoints in each PostgreSQL upsert.",
            buckets=(0, 1, 2, 5, 10, 25, 50, 100, 250),
            registry=registry,
        )
        self.checkpoint_flush_duration = Histogram(
            "reddit_kafka_checkpoint_flush_duration_seconds",
            "Redis-to-PostgreSQL checkpoint flush latency.",
            ("result",),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
            registry=registry,
        )
        self.circuit_breaker_transitions = Counter(
            "reddit_kafka_circuit_breaker_transitions_total",
            "Circuit breaker state transitions.",
            ("state",),
            registry=registry,
        )
        self.cleanup_runs = Counter(
            "reddit_kafka_cleanup_runs_total",
            "Dead-stream cleanup sweeps.",
            ("result",),
            registry=registry,
        )
        self.cleanup_removed = Counter(
            "reddit_kafka_cleanup_removed_streams_total",
            "Terminal registry entries removed by cleanup.",
            registry=registry,
        )
        self.dependency_ready = Gauge(
            "reddit_kafka_dependency_ready",
            "Whether a readiness dependency passed its latest check.",
            ("dependency",),
            registry=registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)


METRICS = ApplicationMetrics()

__all__ = ["CONTENT_TYPE_LATEST", "METRICS", "ApplicationMetrics"]
