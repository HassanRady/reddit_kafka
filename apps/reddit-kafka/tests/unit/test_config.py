import os
import subprocess
import sys


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
