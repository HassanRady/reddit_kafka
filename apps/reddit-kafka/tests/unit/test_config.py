import os
import subprocess
import sys

from src.config import SchemaSettings


def test_postgres_settings_can_be_imported_without_unrelated_credentials() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("REDDIT_", "REDIS_", "KAFKA_", "SCHEMA_"))
    }

    result = subprocess.run(
        [sys.executable, "-c", "from src.config import PostgresSettings"],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_local_schema_mode_has_defaults_and_does_not_require_aws_settings(
    monkeypatch,
) -> None:
    for variable in (
        "SCHEMA_REGISTRY_NAME",
        "SCHEMA_NAME",
        "SCHEMA_VERSION",
        "AWS_REGION",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("USE_AWS_SCHEMA_REGISTRY", "false")

    settings = SchemaSettings()

    assert settings.use_aws_schema_registry is False
    assert settings.schema_name == "RedditComment"
    assert settings.schema_version == 1
