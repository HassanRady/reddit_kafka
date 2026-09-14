from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

import src.app as app_module


@pytest.mark.asyncio
async def test_lifespan_closes_clients_and_flushes_kafka_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = MagicMock()
    settings.db_flush_interval = 30
    settings.dead_stream_cleanup_interval = 30

    redis = AsyncMock()
    reddit_client = MagicMock()
    reddit_client.close = AsyncMock()
    kafka_producer = MagicMock()
    kafka_producer.flush.return_value = 0
    to_thread = AsyncMock(
        side_effect=lambda function, *args, **kwargs: function(*args, **kwargs)
    )
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
    monkeypatch.setattr(app_module.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(app_module, "_reddit_client", reddit_client)
    monkeypatch.setattr(app_module, "_kafka_producer", kafka_producer)

    async with app_module.lifespan(FastAPI()):
        reddit_client.close.assert_not_awaited()
        kafka_producer.flush.assert_not_called()

    reddit_client.close.assert_awaited_once_with()
    to_thread.assert_awaited_once_with(kafka_producer.flush, timeout=5.0)
    kafka_producer.flush.assert_called_once_with(timeout=5.0)
    assert app_module._reddit_client is None
    assert app_module._kafka_producer is None
