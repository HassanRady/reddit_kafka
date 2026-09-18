import asyncio
import contextvars
from datetime import UTC, datetime, timedelta
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

    async def update_status_if_owner(
        self, stream_id: str, status: str, instance_id: str
    ) -> bool:
        del instance_id
        self.statuses.append((stream_id, status))
        return True

    async def request_stop(self, stream_id: str) -> None:
        self.stop_requests.add(stream_id)
        self.statuses.append((stream_id, "stopping"))

    async def is_stop_requested(self, stream_id: str) -> bool:
        return stream_id in self.stop_requests


class RecoverableRegistry:
    def __init__(self) -> None:
        self.meta: dict[str, Any] = {
            "id": "stream-1",
            "subreddit": "python",
            "status": "active",
            "instance_id": "failed-process",
            "config": {},
            "updated_at": "2026-09-17T12:00:00Z",
        }
        self.stop_requests: set[str] = set()

    async def list_streams(self) -> list[dict[str, Any]]:
        return [dict(self.meta)]

    async def get_stream(self, stream_id: str) -> dict[str, Any]:
        assert stream_id == self.meta["id"]
        return dict(self.meta)

    async def update_status(
        self, stream_id: str, status: str, instance_id: str | None = None
    ) -> None:
        assert stream_id == self.meta["id"]
        self.meta["status"] = status
        if instance_id is not None:
            self.meta["instance_id"] = instance_id

    async def update_status_if_owner(
        self, stream_id: str, status: str, instance_id: str
    ) -> bool:
        assert stream_id == self.meta["id"]
        if self.meta["instance_id"] != instance_id:
            return False
        self.meta["status"] = status
        return True

    async def is_stop_requested(self, stream_id: str) -> bool:
        assert stream_id == self.meta["id"]
        return stream_id in self.stop_requests

    async def finalize_stop_if_unowned(
        self, stream_id: str, subreddit: str, expected_updated_at: str
    ) -> bool:
        del stream_id, subreddit, expected_updated_at
        return False


class SharedLockManager:
    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}
        self.counter = 0

    async def is_locked(self, subreddit: str) -> bool:
        return subreddit in self.tokens

    async def acquire_lock(
        self, subreddit: str, instance_id: str, ttl: int = 60
    ) -> str | None:
        del ttl
        if subreddit in self.tokens:
            return None
        self.counter += 1
        token = f"{instance_id}:lease-{self.counter}"
        self.tokens[subreddit] = token
        return token

    async def owns_lock(self, subreddit: str, token: str) -> bool:
        return self.tokens.get(subreddit) == token

    async def release_lock(self, subreddit: str, token: str) -> bool:
        if self.tokens.get(subreddit) != token:
            return False
        del self.tokens[subreddit]
        return True


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


@pytest.mark.asyncio
async def test_orphan_is_adopted_by_exactly_one_instance() -> None:
    registry = RecoverableRegistry()
    locks = SharedLockManager()
    started_by: list[str] = []

    def runner_for(instance_id: str):
        async def runner(subreddit: str, lock_token: str | None) -> None:
            assert lock_token is not None
            started_by.append(instance_id)
            try:
                await asyncio.Event().wait()
            finally:
                await locks.release_lock(subreddit, lock_token)

        return runner

    first = StreamManager(  # type: ignore[arg-type]
        registry, runner_for("process-1"), "process-1", lock_manager=locks
    )
    second = StreamManager(  # type: ignore[arg-type]
        registry, runner_for("process-2"), "process-2", lock_manager=locks
    )

    await asyncio.gather(first.reconcile_once(), second.reconcile_once())
    await asyncio.sleep(0)

    assert len(started_by) == 1
    assert registry.meta["instance_id"] == started_by[0]
    assert (
        sum(
            [
                await first.is_running_locally("stream-1"),
                await second.is_running_locally("stream-1"),
            ]
        )
        == 1
    )

    await first.stop_all()
    await second.stop_all()


@pytest.mark.asyncio
async def test_graceful_instance_shutdown_hands_stream_to_another_instance() -> None:
    registry = RecoverableRegistry()
    locks = SharedLockManager()
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    def runner_for(started: asyncio.Event):
        async def runner(subreddit: str, lock_token: str | None) -> None:
            assert lock_token is not None
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await locks.release_lock(subreddit, lock_token)

        return runner

    first = StreamManager(  # type: ignore[arg-type]
        registry, runner_for(first_started), "process-1", lock_manager=locks
    )
    second = StreamManager(  # type: ignore[arg-type]
        registry, runner_for(second_started), "process-2", lock_manager=locks
    )

    await first.reconcile_once()
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await first.stop_all()

    assert registry.meta["status"] == "active"
    assert not locks.tokens

    await second.reconcile_once()
    await asyncio.wait_for(second_started.wait(), timeout=1)

    assert registry.meta["status"] == "active"
    assert registry.meta["instance_id"] == "process-2"
    assert await second.is_running_locally("stream-1")

    await second.stop_all()


@pytest.mark.asyncio
async def test_worker_does_not_start_if_lease_expires_during_activation() -> None:
    registry = RecoverableRegistry()
    locks = SharedLockManager()
    runner = AsyncMock()
    manager = StreamManager(  # type: ignore[arg-type]
        registry, runner, "process-1", lock_manager=locks
    )
    token = await locks.acquire_lock("python", "process-1")
    assert token is not None
    original_update_status = registry.update_status

    async def update_status_then_lose_lease(
        stream_id: str, status: str, instance_id: str | None = None
    ) -> None:
        await original_update_status(stream_id, status, instance_id)
        locks.tokens["python"] = "process-2:successor-lease"

    registry.update_status = update_status_then_lose_lease  # type: ignore[method-assign]

    started = await manager._start_local_worker(
        "stream-1", "python", token, transition="reconcile"
    )

    assert started is False
    assert not await manager.is_running_locally("stream-1")
    runner.assert_not_awaited()


def test_fresh_starting_stream_is_not_reconciled() -> None:
    meta = {
        "status": "starting",
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }

    assert StreamManager._needs_reconciliation(meta) is False


def test_stale_starting_stream_is_reconciled() -> None:
    meta = {
        "status": "starting",
        "updated_at": (datetime.now(UTC) - timedelta(seconds=61))
        .isoformat()
        .replace("+00:00", "Z"),
    }

    assert StreamManager._needs_reconciliation(meta) is True
