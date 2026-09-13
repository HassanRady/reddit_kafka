from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio as redis


@pytest.fixture
def redis_mock():
    """Mock Redis client for testing with async support."""
    mock = AsyncMock(spec=redis.Redis)
    mock.sadd = AsyncMock()
    mock.set = AsyncMock()
    mock.get = AsyncMock()
    mock.hset = AsyncMock()
    mock.hgetall = AsyncMock()
    mock.keys = AsyncMock()
    mock.delete = AsyncMock()
    mock.lpush = AsyncMock()
    mock.ltrim = AsyncMock()
    mock.expire = AsyncMock()
    mock.llen = AsyncMock()
    mock.lrange = AsyncMock()
    pipeline_mock = MagicMock()
    pipeline_mock.hgetall = MagicMock()
    pipeline_mock.execute = AsyncMock(return_value=[])
    mock.pipeline = MagicMock(return_value=pipeline_mock)
    mock.scan_iter = MagicMock(return_value=_empty_async_iter())
    return mock


async def _empty_async_iter():
    if False:
        yield None


@pytest.fixture
def session_mock():
    """Mock SQLAlchemy async session."""
    mock = AsyncMock()
    mock.execute = AsyncMock()
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    return mock


class AsyncContextManagerMock:
    """Helper class to create proper async context managers in tests."""

    def __init__(self, session_mock):
        self.session_mock = session_mock

    async def __aenter__(self):
        return self.session_mock

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def session_maker_mock(session_mock):
    """Mock SQLAlchemy async session maker that supports assertions
    and async context manager."""
    return MagicMock(side_effect=lambda: AsyncContextManagerMock(session_mock))


# Enable pytest-asyncio
pytest_plugins = ("pytest_asyncio",)
