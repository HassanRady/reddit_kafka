import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection

from src.config import PostgresSettings
from src.db import close_db, get_engine, init_db

MAX_MIGRATION_WAIT_SECONDS = 600
MIGRATION_RETRY_DELAY_SECONDS = 10
MIGRATION_LOCK_ID = 728_194_632

_CREATE_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(255) PRIMARY KEY,
    checksum VARCHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _is_retryable_migration_error(error: Exception) -> bool:
    retryable_types = (
        ConnectionError,
        TimeoutError,
        OSError,
        OperationalError,
        DBAPIError,
    )
    if isinstance(error, retryable_types):
        return True

    message = str(error).lower()
    retryable_messages = (
        "could not connect",
        "connection refused",
        "connection timed out",
        "server closed the connection",
        "too many connections",
        "the database system is starting up",
    )
    return any(token in message for token in retryable_messages)


async def _run_with_retry(operation: Callable[[], Awaitable[None]]) -> None:
    deadline = monotonic() + MAX_MIGRATION_WAIT_SECONDS

    while True:
        try:
            await operation()
            return
        except Exception as error:
            await close_db()

            if not _is_retryable_migration_error(error):
                raise

            remaining_seconds = deadline - monotonic()
            if remaining_seconds <= 0:
                raise TimeoutError(
                    "Migration database setup did not succeed within "
                    f"{MAX_MIGRATION_WAIT_SECONDS} seconds"
                ) from error

            sleep_seconds = min(MIGRATION_RETRY_DELAY_SECONDS, remaining_seconds)
            print(
                f"Migration attempt failed: {error}. "
                f"Retrying in {sleep_seconds:.0f}s..."
            )
            await asyncio.sleep(sleep_seconds)


async def _execute_sql_script(connection: AsyncConnection, raw_sql: str) -> None:
    """Execute a complete SQL file without splitting it into statements."""
    if not raw_sql.strip():
        return

    raw_connection = await connection.get_raw_connection()
    driver_connection: Any = raw_connection.driver_connection
    await driver_connection.execute(raw_sql)


async def _apply_migration_files(
    connection: AsyncConnection, migration_files: list[Path]
) -> None:
    """Apply pending migrations and record their checksums atomically."""
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": MIGRATION_LOCK_ID},
    )
    await connection.execute(text(_CREATE_SCHEMA_MIGRATIONS_SQL))

    result = await connection.execute(
        text("SELECT version, checksum FROM schema_migrations")
    )
    applied = {row["version"]: row["checksum"] for row in result.mappings()}

    for migration_file in migration_files:
        version = migration_file.name
        raw_bytes = migration_file.read_bytes()
        raw_sql = raw_bytes.decode("utf-8")
        checksum = hashlib.sha256(raw_bytes).hexdigest()

        if version in applied:
            if applied[version] != checksum:
                raise RuntimeError(
                    f"Applied migration {version} has been modified; "
                    "create a new migration instead"
                )
            print(f"Skipping migration already applied: {version}")
            continue

        print(f"Running migration: {version}")
        await _execute_sql_script(connection, raw_sql)
        await connection.execute(
            text(
                """
                INSERT INTO schema_migrations (version, checksum)
                VALUES (:version, :checksum)
                """
            ),
            {"version": version, "checksum": checksum},
        )
        print(f"✓ {version} completed")


async def run_migrations(settings: PostgresSettings) -> None:
    """Run all SQL migration files in migrations/ directory."""
    migrations_dir = Path(__file__).parent.parent / "migrations"
    migration_files = sorted(migrations_dir.glob("*.sql"))

    async def execute_migrations() -> None:
        await init_db(settings)

        engine = get_engine()
        if engine is None:
            raise RuntimeError("Engine not initialized")

        async with engine.begin() as conn:
            await _apply_migration_files(conn, migration_files)

    await _run_with_retry(execute_migrations)


async def create_tables_from_models(settings: PostgresSettings) -> None:
    """Alternative: create tables from ORM models (requires Base.metadata)."""
    from src.models import Base

    async def create_tables() -> None:
        await init_db(settings)

        engine = get_engine()
        if engine is None:
            raise RuntimeError("Engine not initialized")

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            print("✓ All tables created from models")

    await _run_with_retry(create_tables)


if __name__ == "__main__":
    settings = PostgresSettings()
    asyncio.run(run_migrations(settings))
