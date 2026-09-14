import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.repositories.stream_registry import (
    _DELETE_STREAM_IF_UNCHANGED_SCRIPT,
    StreamExistsError,
    StreamNotFoundError,
    StreamRegistry,
    _now_iso,
)


class TestNowIso:
    def test_now_iso_format(self):
        ts = _now_iso()
        assert isinstance(ts, str)
        assert ts.endswith("Z")
        parsed = datetime.fromisoformat(ts.rstrip("Z"))
        assert parsed.tzinfo is None

    def test_now_iso_is_naive_utc(self):
        ts = _now_iso()
        parsed = datetime.fromisoformat(ts.rstrip("Z"))
        # Verify it's recent (within last minute)
        now = datetime.now(UTC).replace(tzinfo=None)
        diff = (now - parsed).total_seconds()
        assert 0 <= diff <= 60


@pytest_asyncio.fixture
async def registry(redis_mock, session_maker_mock):
    return StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)


class TestStreamRegistryCreateStream:
    """Test create_stream method."""

    @pytest.mark.asyncio
    async def test_create_stream_success(self, redis_mock, session_maker_mock):
        """Test successful stream creation."""
        redis_mock.set = AsyncMock(return_value=True)
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.hset = AsyncMock()

        registry = StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)

        meta = await registry.create_stream(
            subreddit="python",
            config={"limit": 100},
            instance_id="instance-1",
        )

        assert redis_mock.set.await_count == 2
        redis_mock.hset.assert_called_once()

        assert meta["subreddit"] == "python"
        assert meta["status"] == "starting"
        assert meta["instance_id"] == "instance-1"
        assert json.loads(meta["config"]) == {"limit": 100}
        assert meta["created_at"].endswith("Z")
        assert meta["updated_at"].endswith("Z")

    @pytest.mark.asyncio
    async def test_create_stream_duplicate_raises_error(
        self, redis_mock, session_maker_mock
    ):
        redis_mock.get = AsyncMock(return_value="existing-stream-id")

        registry = StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)

        with pytest.raises(StreamExistsError):
            await registry.create_stream(subreddit="python")

    @pytest.mark.asyncio
    async def test_create_stream_persists_to_postgres(
        self, redis_mock, session_mock, session_maker_mock
    ):
        redis_mock.set = AsyncMock(return_value=True)
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.hset = AsyncMock()

        registry = StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)

        await registry.create_stream(
            subreddit="python",
            config={"limit": 50},
            instance_id="instance-1",
        )

        session_maker_mock.assert_called()
        session_mock.execute.assert_called()
        session_mock.commit.assert_called()

    @pytest.mark.asyncio
    async def test_create_stream_without_session_maker(self, redis_mock):
        """Test stream creation without session maker (Redis-only mode)."""
        redis_mock.set = AsyncMock(return_value=True)
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.hset = AsyncMock()

        registry = StreamRegistry(redis=redis_mock, session_maker=None)

        meta = await registry.create_stream(subreddit="python")

        assert meta["subreddit"] == "python"
        redis_mock.hset.assert_called_once()


class TestStreamRegistryGetStream:
    """Test get_stream method."""

    @pytest.mark.asyncio
    async def test_get_stream_success(self, redis_mock, session_maker_mock):
        stream_data = {
            "id": "stream-1",
            "subreddit": "python",
            "status": "running",
            "instance_id": "instance-1",
            "config": json.dumps({"limit": 100}),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        redis_mock.hgetall = AsyncMock(return_value=stream_data)

        registry = StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)

        meta = await registry.get_stream("stream-1")

        assert meta["id"] == "stream-1"
        assert meta["subreddit"] == "python"
        assert meta["config"] == {"limit": 100}
        redis_mock.hgetall.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_stream_not_found(self, redis_mock, session_maker_mock):
        redis_mock.hgetall = AsyncMock(return_value={})

        registry = StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)

        with pytest.raises(StreamNotFoundError):
            await registry.get_stream("nonexistent")


class TestStreamRegistryCheckpoint:
    """Test checkpoint operations."""

    @pytest.mark.asyncio
    async def test_set_checkpoint(self, redis_mock, session_maker_mock):
        redis_mock.hset = AsyncMock()

        registry = StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)

        await registry.set_checkpoint(
            stream_id="stream-1",
            last_comment_id="abc123",
            last_processed_at="2026-05-05T10:00:00Z",
        )

        redis_mock.hset.assert_called_once()
        call_args = redis_mock.hset.call_args
        # Verify the mapping contains the checkpoint data
        assert call_args[1]["mapping"]["last_comment_id"] == "abc123"
        assert call_args[1]["mapping"]["last_processed_at"] == "2026-05-05T10:00:00Z"
        assert "updated_at" in call_args[1]["mapping"]

    @pytest.mark.asyncio
    async def test_get_checkpoint(self, redis_mock, session_maker_mock):
        """Test retrieving checkpoint data."""
        checkpoint_data = {
            "last_comment_id": "abc123",
            "last_processed_at": "2026-05-05T10:00:00Z",
            "updated_at": _now_iso(),
        }
        redis_mock.hgetall = AsyncMock(return_value=checkpoint_data)

        registry = StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)

        checkpoint = await registry.get_checkpoint("stream-1")

        assert checkpoint["last_comment_id"] == "abc123"
        assert checkpoint["last_processed_at"] == "2026-05-05T10:00:00Z"

    @pytest.mark.asyncio
    async def test_get_checkpoint_empty(self, redis_mock, session_maker_mock):
        """Test retrieving checkpoint when none exists."""
        redis_mock.hgetall = AsyncMock(return_value={})

        registry = StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)

        checkpoint = await registry.get_checkpoint("stream-1")

        assert checkpoint["last_comment_id"] is None
        assert checkpoint["last_processed_at"] is None


class TestStreamRegistryDelete:
    """Test delete_stream method."""

    @pytest.mark.asyncio
    async def test_delete_stream(self, redis_mock, session_maker_mock):
        """Test deleting a stream."""
        stream_data = {
            "id": "stream-1",
            "subreddit": "python",
        }
        redis_mock.hgetall = AsyncMock(return_value=stream_data)

        redis_mock.eval = AsyncMock(return_value=1)

        registry = StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)

        deleted = await registry.delete_stream(
            "stream-1",
            expected_status="stopped",
            expected_updated_at="2026-05-05T10:00:00Z",
        )

        assert deleted is True
        redis_mock.eval.assert_awaited_once_with(
            _DELETE_STREAM_IF_UNCHANGED_SCRIPT,
            6,
            "stream:meta:stream-1",
            "stream:checkpoint:stream-1",
            "stream:stop-request:stream-1",
            "stream:subreddit:python",
            "streams:all",
            "stream:identity:python",
            "stream-1",
            "stopped",
            "2026-05-05T10:00:00Z",
            "0",
        )
        redis_mock.delete.assert_not_awaited()


class TestStreamRegistryUpdateStatus:
    """Test update_status method."""

    @pytest.mark.asyncio
    async def test_update_status(self, redis_mock, session_mock, session_maker_mock):
        """Test updating stream status."""
        redis_mock.hset = AsyncMock()

        registry = StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)

        await registry.update_status("stream-1", "running", instance_id="instance-1")

        redis_mock.hset.assert_called_once()
        call_args = redis_mock.hset.call_args
        assert call_args[1]["mapping"]["status"] == "running"
        assert call_args[1]["mapping"]["instance_id"] == "instance-1"

        session_mock.execute.assert_called()
        session_mock.commit.assert_called()


class TestStreamRegistryStopRequest:
    @pytest.mark.asyncio
    async def test_request_stop_is_shared_and_marks_stopping(
        self, redis_mock, session_maker_mock
    ):
        redis_mock.hgetall = AsyncMock(
            return_value={
                "id": "stream-1",
                "instance_id": "process-1",
                "config": "{}",
            }
        )
        registry = StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)

        await registry.request_stop("stream-1")

        redis_mock.set.assert_awaited_once()
        assert redis_mock.set.await_args.args[0] == "stream:stop-request:stream-1"
        status_mapping = redis_mock.hset.await_args.kwargs["mapping"]
        assert status_mapping["status"] == "stopping"
        assert status_mapping["instance_id"] == "process-1"

    @pytest.mark.asyncio
    async def test_is_stop_requested(self, redis_mock, session_maker_mock):
        redis_mock.exists = AsyncMock(return_value=1)
        registry = StreamRegistry(redis=redis_mock, session_maker=session_maker_mock)

        assert await registry.is_stop_requested("stream-1") is True
        redis_mock.exists.assert_awaited_once_with("stream:stop-request:stream-1")


class MagicMockWrapper:
    """Helper wrapper for testing."""

    pass
