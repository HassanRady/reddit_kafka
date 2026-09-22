class KafkaDeliveryError(RuntimeError):
    """Raised when Kafka cannot accept or deliver a produced message."""


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and a caller must wait before retrying."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Circuit breaker OPEN. Retry in {retry_after}s")


class LockLostError(RuntimeError):
    """Raised when a worker can no longer prove ownership of its lease."""
