import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.migrations import _apply_migration_files


def _migration_connection(
    applied: list[dict[str, str]] | None = None,
) -> tuple[MagicMock, AsyncMock]:
    result = MagicMock()
    result.mappings.return_value = applied or []

    async def execute(statement, parameters=None):
        del parameters
        if "SELECT version, checksum" in str(statement):
            return result
        return MagicMock()

    driver_execute = AsyncMock()
    raw_connection = MagicMock()
    raw_connection.driver_connection.execute = driver_execute
    connection = MagicMock()
    connection.execute = AsyncMock(side_effect=execute)
    connection.get_raw_connection = AsyncMock(return_value=raw_connection)
    return connection, driver_execute


@pytest.mark.asyncio
async def test_applies_complete_sql_file_without_splitting(tmp_path: Path) -> None:
    migration = tmp_path / "002_create_function.sql"
    sql = """
    CREATE FUNCTION example() RETURNS void AS $$
    BEGIN
        RAISE NOTICE 'first;second';
    END;
    $$ LANGUAGE plpgsql;
    """
    migration.write_text(sql)
    connection, driver_execute = _migration_connection()

    await _apply_migration_files(connection, [migration])

    driver_execute.assert_awaited_once_with(sql)
    insert_calls = [
        call
        for call in connection.execute.await_args_list
        if "INSERT INTO schema_migrations" in str(call.args[0])
    ]
    assert len(insert_calls) == 1
    assert insert_calls[0].args[1] == {
        "version": migration.name,
        "checksum": hashlib.sha256(sql.encode()).hexdigest(),
    }


@pytest.mark.asyncio
async def test_skips_migration_with_matching_checksum(tmp_path: Path) -> None:
    migration = tmp_path / "001_initial.sql"
    sql = "CREATE TABLE example (id INTEGER);"
    migration.write_text(sql)
    applied = [
        {
            "version": migration.name,
            "checksum": hashlib.sha256(sql.encode()).hexdigest(),
        }
    ]
    connection, driver_execute = _migration_connection(applied)

    await _apply_migration_files(connection, [migration])

    driver_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejects_modified_applied_migration(tmp_path: Path) -> None:
    migration = tmp_path / "001_initial.sql"
    migration.write_text("SELECT 2;")
    original_checksum = hashlib.sha256(b"SELECT 1;").hexdigest()
    connection, driver_execute = _migration_connection(
        [{"version": migration.name, "checksum": original_checksum}]
    )

    with pytest.raises(RuntimeError, match="has been modified"):
        await _apply_migration_files(connection, [migration])

    driver_execute.assert_not_awaited()
