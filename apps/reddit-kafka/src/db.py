from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import PostgresSettings
from src.observability.telemetry import instrument_sqlalchemy

_engine: AsyncEngine | None = None
_async_session_maker: async_sessionmaker[AsyncSession] | None = None


def build_postgres_url(settings: PostgresSettings) -> str:
    return (
        f"postgresql+asyncpg://{settings.user}:{settings.password.get_secret_value()}"
        f"@{settings.host}:{settings.port}/{settings.db}"
    )


async def init_db(settings: PostgresSettings) -> None:
    global _engine, _async_session_maker

    url = build_postgres_url(settings)
    _engine = create_async_engine(
        url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )
    instrument_sqlalchemy(_engine)

    _async_session_maker = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    if _async_session_maker is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    async with _async_session_maker() as session:
        yield session


async def close_db() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def get_engine() -> AsyncEngine | None:
    return _engine
