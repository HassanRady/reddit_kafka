"""Unit tests for CheckpointFlusher."""

import uuid
from unittest.mock import AsyncMock

import pytest

from src.tasks.checkpoint_flusher import CheckpointFlusher


class TestCheckpointFlusherFlush:
    """Test flush method."""

    @pytest.mark.asyncio
    async def test_flush_no_checkpoints(self, redis_mock, session_maker_mock):
        flusher = CheckpointFlusher(
            redis_client=redis_mock,
            session_maker=session_maker_mock,
            instance_id="instance-1",
        )
        await flusher.flush()
        redis_mock.smembers.assert_awaited_once_with("streams:instance:instance-1")
        redis_mock.scan_iter.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_single_checkpoint(
        self, redis_mock, session_mock, session_maker_mock
    ):
        stream_id = str(uuid.uuid4())
        redis_mock.smembers.return_value = {stream_id}
        redis_mock.pipeline.return_value.execute = AsyncMock(
            return_value=[
                {"instance_id": "instance-1"},
                {
                    "last_comment_id": "abc123",
                    "last_processed_at": "2026-05-05T10:00:00Z",
                },
            ]
        )
        flusher = CheckpointFlusher(
            redis_client=redis_mock,
            session_maker=session_maker_mock,
            instance_id="instance-1",
        )
        await flusher.flush()
        redis_mock.scan_iter.assert_not_called()
        session_mock.execute.assert_awaited_once()
        parameters = session_mock.execute.call_args.args[1]
        assert len(parameters) == 1
        assert parameters[0]["stream_id"] == stream_id

    @pytest.mark.asyncio
    async def test_flush_strips_timezone(
        self, redis_mock, session_mock, session_maker_mock
    ):
        stream_id = str(uuid.uuid4())
        redis_mock.smembers.return_value = {stream_id}
        redis_mock.pipeline.return_value.execute = AsyncMock(
            return_value=[
                {"instance_id": "instance-1"},
                {
                    "last_comment_id": "abc123",
                    "last_processed_at": "2026-05-05T10:00:00+00:00",
                },
            ]
        )
        flusher = CheckpointFlusher(
            redis_client=redis_mock,
            session_maker=session_maker_mock,
            instance_id="instance-1",
        )
        await flusher.flush()
        call_args = session_mock.execute.call_args
        params = call_args[0][1][0]
        ts = params.get("last_processed_at")
        assert ts.tzinfo is None

    @pytest.mark.asyncio
    async def test_flush_batches_multiple_checkpoints_in_one_execute(
        self, redis_mock, session_mock, session_maker_mock
    ):
        stream_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        redis_mock.smembers.return_value = set(stream_ids)
        redis_mock.pipeline.return_value.execute = AsyncMock(
            return_value=[
                {"instance_id": "instance-1"},
                {"last_comment_id": "comment-1"},
                {"instance_id": "instance-1"},
                {"last_comment_id": "comment-2"},
            ]
        )
        flusher = CheckpointFlusher(
            redis_client=redis_mock,
            session_maker=session_maker_mock,
            instance_id="instance-1",
        )

        await flusher.flush()

        session_mock.execute.assert_awaited_once()
        parameters = session_mock.execute.call_args.args[1]
        assert {item["stream_id"] for item in parameters} == set(stream_ids)
        session_mock.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_flush_removes_streams_owned_by_another_instance(
        self, redis_mock, session_mock, session_maker_mock
    ):
        redis_mock.smembers.return_value = ["local-stream", "moved-stream"]
        redis_mock.pipeline.return_value.execute = AsyncMock(
            return_value=[
                {"instance_id": "instance-1"},
                {"last_comment_id": "comment-1"},
                {"instance_id": "instance-2"},
                {"last_comment_id": "comment-2"},
            ]
        )
        flusher = CheckpointFlusher(
            redis_client=redis_mock,
            session_maker=session_maker_mock,
            instance_id="instance-1",
        )

        await flusher.flush()

        parameters = session_mock.execute.call_args.args[1]
        assert [item["stream_id"] for item in parameters] == ["local-stream"]
        redis_mock.srem.assert_awaited_once_with(
            "streams:instance:instance-1", "moved-stream"
        )
