import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import monotonic

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from src.config import PostgresSettings
from src.db import close_db, get_engine, init_db

MAX_MIGRATION_WAIT_SECONDS = 600
MIGRATION_RETRY_DELAY_SECONDS = 10


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
            for migration_file in migration_files:
                print(f"Running migration: {migration_file.name}")
                raw_sql = migration_file.read_text()
                statements = [
                    stmt.strip() for stmt in raw_sql.split(";") if stmt.strip()
                ]

                for statement in statements:
                    await conn.execute(text(statement))
                print(f"✓ {migration_file.name} completed")

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
