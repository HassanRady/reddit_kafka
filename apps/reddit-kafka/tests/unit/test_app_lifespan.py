from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

import src.app as app_module


@pytest.mark.asyncio
async def test_lifespan_closes_reddit_client_and_resets_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = MagicMock()
    settings.db_flush_interval = 30
    settings.dead_stream_cleanup_interval = 30

    redis = AsyncMock()
    reddit_client = MagicMock()
    reddit_client.close = AsyncMock()
    manager = MagicMock()
    manager.stop_all = AsyncMock()
    flusher = MagicMock()
    flusher.start = AsyncMock()
    flusher.flush = AsyncMock()
    flusher.stop = AsyncMock()
    cleanup = MagicMock()
    cleanup.start = AsyncMock()
    cleanup.stop = AsyncMock()

    monkeypatch.setattr(app_module, "Settings", MagicMock(return_value=settings))
    monkeypatch.setattr(app_module, "init_db", AsyncMock())
    monkeypatch.setattr(app_module, "get_redis", MagicMock(return_value=redis))
    monkeypatch.setattr(app_module, "StreamRegistry", MagicMock())
    monkeypatch.setattr(app_module, "DistributedLockManager", MagicMock())
    monkeypatch.setattr(
        app_module, "_get_reddit_client", MagicMock(return_value=reddit_client)
    )
    monkeypatch.setattr(app_module, "_create_runner", AsyncMock())
    monkeypatch.setattr(app_module, "StreamManager", MagicMock(return_value=manager))
    monkeypatch.setattr(app_module, "get_engine", MagicMock(return_value=object()))
    monkeypatch.setattr(
        app_module, "CheckpointFlusher", MagicMock(return_value=flusher)
    )
    monkeypatch.setattr(
        app_module, "DeadStreamCleanup", MagicMock(return_value=cleanup)
    )
    monkeypatch.setattr(app_module, "close_redis", AsyncMock())
    monkeypatch.setattr(app_module, "close_db", AsyncMock())
    monkeypatch.setattr(app_module, "_reddit_client", reddit_client)

    async with app_module.lifespan(FastAPI()):
        reddit_client.close.assert_not_awaited()

    reddit_client.close.assert_awaited_once_with()
    assert app_module._reddit_client is None
