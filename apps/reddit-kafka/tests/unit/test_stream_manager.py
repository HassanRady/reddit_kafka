import asyncio
import contextvars
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.stream.manager import StreamManager


class SharedRegistry:
    """Minimal shared registry used to model separate Uvicorn processes."""

    def __init__(self) -> None:
        self.stop_requests: set[str] = set()
        self.statuses: list[tuple[str, str]] = []

    async def create_stream(
        self,
        subreddit: str,
        config: dict[str, Any] | None = None,
        instance_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": "stream-1",
            "subreddit": subreddit,
            "status": "starting",
            "instance_id": instance_id or "",
            "config": config or {},
        }

    async def update_status(
        self, stream_id: str, status: str, instance_id: str | None = None
    ) -> None:
        self.statuses.append((stream_id, status))

    async def request_stop(self, stream_id: str) -> None:
        self.stop_requests.add(stream_id)
        self.statuses.append((stream_id, "stopping"))

    async def is_stop_requested(self, stream_id: str) -> bool:
        return stream_id in self.stop_requests


@pytest.mark.asyncio
async def test_stream_task_does_not_inherit_request_context() -> None:
    registry = SharedRegistry()
    request_context: contextvars.ContextVar[str | None] = contextvars.ContextVar(
        "test_request_context",
        default=None,
    )
    observed_context: list[str | None] = []
    runner_started = asyncio.Event()

    async def runner(subreddit: str, lock_token: str | None) -> None:
        del subreddit, lock_token
        observed_context.append(request_context.get())
        runner_started.set()
        await asyncio.Event().wait()

    manager = StreamManager(  # type: ignore[arg-type]
        registry,
        runner,
        "process-1",
        stop_poll_interval=0.01,
    )
    token = request_context.set("request-123")
    try:
        meta = await manager.start_stream("python")
    finally:
        request_context.reset(token)

    await asyncio.wait_for(runner_started.wait(), timeout=1)
    await manager.stop_stream(meta["id"])

    assert observed_context == [None]


@pytest.mark.asyncio
async def test_non_local_stop_cancels_runner_in_owning_manager() -> None:
    registry = SharedRegistry()
    runner_started = asyncio.Event()
    runner_cancelled = asyncio.Event()

    async def owner_runner(subreddit: str, lock_token: str | None) -> None:
        del subreddit, lock_token
        runner_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            runner_cancelled.set()
            raise

    owner = StreamManager(  # type: ignore[arg-type]
        registry,
        owner_runner,
        "process-1",
        stop_poll_interval=0.01,
    )
    other_process = StreamManager(  # type: ignore[arg-type]
        registry,
        AsyncMock(),
        "process-2",
        stop_poll_interval=0.01,
    )

    meta = await owner.start_stream("python")
    await asyncio.wait_for(runner_started.wait(), timeout=1)

    await other_process.stop_stream(meta["id"])

    await asyncio.wait_for(runner_cancelled.wait(), timeout=1)
    for _ in range(100):
        if not await owner.is_running_locally(meta["id"]):
            break
        await asyncio.sleep(0.01)

    assert not await owner.is_running_locally(meta["id"])
    assert (meta["id"], "stopping") in registry.statuses
    assert registry.statuses[-1] == (meta["id"], "stopped")


@pytest.mark.asyncio
async def test_non_local_stop_does_not_mark_stream_stopped_locally() -> None:
    registry = AsyncMock()
    manager = StreamManager(registry, AsyncMock(), "process-2")

    await manager.stop_stream("stream-1")

    registry.request_stop.assert_awaited_once_with("stream-1")
    registry.update_status.assert_not_awaited()
