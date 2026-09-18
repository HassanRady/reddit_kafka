class KafkaDeliveryError(RuntimeError):
    """Raised when Kafka cannot accept or deliver a produced message."""


class LockLostError(RuntimeError):
    """Raised when a worker can no longer prove ownership of its lease."""
