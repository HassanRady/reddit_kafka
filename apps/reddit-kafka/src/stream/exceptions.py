class KafkaDeliveryError(RuntimeError):
    """Raised when Kafka cannot accept or deliver a produced message."""
