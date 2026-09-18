from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class RedditSettings(BaseSettings):
    client_id: str = Field(alias="REDDIT_CLIENT_ID")
    client_secret: SecretStr = Field(alias="REDDIT_CLIENT_SECRET")
    user_agent: str = Field(alias="REDDIT_USER_AGENT")


class KafkaSettings(BaseSettings):
    bootstrap_servers: str = Field(alias="KAFKA_BOOTSTRAP_SERVERS")
    raw_text_topic: str = Field(alias="KAFKA_RAW_TEXT_TOPIC")
    security_protocol: str = Field("PLAINTEXT", alias="KAFKA_SECURITY_PROTOCOL")
    sasl_mechanism: str = Field("SCRAM-SHA-512", alias="KAFKA_SASL_MECHANISM")
    sasl_username: str | None = Field(default=None, alias="KAFKA_SASL_USERNAME")
    sasl_password: SecretStr | None = Field(default=None, alias="KAFKA_SASL_PASSWORD")
    ssl_ca_location: str | None = Field(default=None, alias="KAFKA_SSL_CA_LOCATION")


class RedisSettings(BaseSettings):
    host: str = Field(alias="REDIS_HOST")
    port: int = Field(alias="REDIS_PORT")
    user: str = Field(alias="REDIS_USER")
    password: SecretStr = Field(alias="REDIS_PASSWORD")
    use_ssl: bool = Field(False, alias="REDIS_USE_SSL")


class PostgresSettings(BaseSettings):
    host: str = Field(alias="POSTGRES_HOST")
    port: int = Field(alias="POSTGRES_PORT")
    user: str = Field(alias="POSTGRES_USER")
    password: SecretStr = Field(alias="POSTGRES_PASSWORD")
    db: str = Field(alias="POSTGRES_DB")


class SchemaSettings(BaseSettings):
    registry_name: str = Field(alias="SCHEMA_REGISTRY_NAME")
    schema_name: str = Field(alias="SCHEMA_NAME")
    schema_version: int = Field(alias="SCHEMA_VERSION")
    aws_region: str = Field(alias="AWS_REGION")
    use_localstack: bool = Field(False, alias="USE_LOCALSTACK")


class ObservabilitySettings(BaseSettings):
    service_name: str = Field("reddit-kafka", alias="OTEL_SERVICE_NAME")
    service_version: str = Field("0.1.0", alias="SERVICE_VERSION")
    environment: str = Field("development", alias="DEPLOYMENT_ENVIRONMENT")
    otlp_endpoint: str | None = Field(None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    traces_enabled: bool = Field(True, alias="TRACES_ENABLED")
    logs_export_enabled: bool = Field(True, alias="OTEL_LOGS_EXPORT_ENABLED")
    trace_sample_ratio: float = Field(
        1.0,
        alias="OTEL_TRACE_SAMPLE_RATIO",
        ge=0.0,
        le=1.0,
    )
    json_logs: bool = Field(True, alias="JSON_LOGS")
    log_level: str = Field("INFO", alias="LOG_LEVEL")


class Settings(BaseSettings):
    db_flush_interval: int = Field(10, alias="DB_FLUSH_INTERVAL")
    dead_stream_cleanup_interval: int = Field(120, alias="DEAD_STREAM_CLEANUP_INTERVAL")
    stream_reconcile_interval: int = Field(10, alias="STREAM_RECONCILE_INTERVAL", gt=0)

    reddit: RedditSettings = Field(default_factory=RedditSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    schema_settings: SchemaSettings = Field(default_factory=SchemaSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
