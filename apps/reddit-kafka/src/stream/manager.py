"""StreamManager that starts/stops per-subreddit streaming tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from src.repositories.stream_registry import StreamNotFoundError, StreamRegistry
from src.stream.lock import DistributedLockManager

logger = logging.getLogger(__name__)


class StreamManager:
    """Manage lifecycle of streams on the local instance.

    A StreamManager does not itself implement the streaming loop; instead it
    accepts a `runner` callable (subreddit, lock token -> awaitable) which performs the
    actual work for a stream. This keeps the manager testable and decoupled.

    Example runner signature: async def runner(subreddit: str, lock_token: str): ...
    """

    def __init__(
        self,
        registry: StreamRegistry,
        runner: Callable[[str, str | None], Awaitable[None]],
        instance_id: str,
        lock_manager: DistributedLockManager | None = None,
        stop_poll_interval: float = 1.0,
    ):
        self.registry = registry
        self.runner = runner
        self.instance_id = instance_id
        # Optional lock manager to prevent duplicate streams across instances.
        self.lock_manager: DistributedLockManager | None = lock_manager
        self.stop_poll_interval = stop_poll_interval
        # local map of stream_id -> asyncio.Task
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # protects _tasks
        self._lock = asyncio.Lock()

    async def start_stream(
        self, subreddit: str, config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create registry entry, acquire lock, and start runner task for subreddit.

        Raises StreamExistsError if a stream for the subreddit already exists.
        Raises RuntimeError if lock cannot be acquired.
        Returns the stream metadata dict from the registry.
        """
        meta = await self.registry.create_stream(
            subreddit, config=config, instance_id=self.instance_id
        )
        stream_id = meta["id"]

        # Try to acquire distributed lock
        lock_token: str | None = None
        if self.lock_manager:
            lock_token = await self.lock_manager.acquire_lock(
                subreddit, self.instance_id, ttl=60
            )
            if lock_token is None:
                # Cleanup registry entry since lock failed
                await self.registry.delete_stream(stream_id)
                raise RuntimeError(f"Cannot acquire lock for subreddit {subreddit}")

        async with self._lock:
            if stream_id in self._tasks:
                logger.warning("stream %s already running locally", stream_id)
                return meta

            task = asyncio.create_task(
                self._run(stream_id, subreddit, lock_token), name=f"stream-{stream_id}"
            )
            self._tasks[stream_id] = task
        await self.registry.update_status(
            stream_id, "active", instance_id=self.instance_id
        )
        return meta

    async def _run(
        self, stream_id: str, subreddit: str, lock_token: str | None
    ) -> None:
        """Wrapper around the runner, handling lifecycle updates and errors."""
        runner_task: asyncio.Task[None] = asyncio.create_task(
            self._invoke_runner(subreddit, lock_token),
            name=f"stream-runner-{stream_id}",
        )
        stop_watcher = asyncio.create_task(
            self._wait_for_stop_request(stream_id),
            name=f"stream-stop-watcher-{stream_id}",
        )
        try:
            logger.info("starting runner for %s (id=%s)", subreddit, stream_id)
            done, _ = await asyncio.wait(
                {runner_task, stop_watcher}, return_when=asyncio.FIRST_COMPLETED
            )

            if stop_watcher in done and stop_watcher.result():
                logger.info("shared stop requested for stream %s", stream_id)
                runner_task.cancel()
                with suppress(asyncio.CancelledError):
                    await runner_task
            else:
                await runner_task

            # runner returned normally - mark stopped
            await self.registry.update_status(stream_id, "stopped")
            logger.info("runner finished for %s (id=%s)", subreddit, stream_id)
        except asyncio.CancelledError:
            # graceful cancellation
            runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await runner_task
            await self.registry.update_status(stream_id, "stopped")
            logger.info("runner cancelled for %s (id=%s)", subreddit, stream_id)
            raise
        except StreamNotFoundError:
            await self.registry.update_status(stream_id, "error")
            logger.exception("stream not found in registry during run: %s", stream_id)
        except Exception:
            await self.registry.update_status(stream_id, "error")
            logger.exception("stream %s crashed", stream_id)
        finally:
            if not runner_task.done():
                runner_task.cancel()
                with suppress(asyncio.CancelledError):
                    await runner_task
            stop_watcher.cancel()
            with suppress(asyncio.CancelledError):
                await stop_watcher
            async with self._lock:
                if stream_id in self._tasks:
                    del self._tasks[stream_id]

    async def _invoke_runner(self, subreddit: str, lock_token: str | None) -> None:
        """Adapt the injected Awaitable factory to an asyncio coroutine."""
        await self.runner(subreddit, lock_token)

    async def _wait_for_stop_request(self, stream_id: str) -> bool:
        """Poll shared state until another process requests termination."""
        while True:
            if await self.registry.is_stop_requested(stream_id):
                return True
            await asyncio.sleep(self.stop_poll_interval)

    async def stop_stream(self, stream_id: str) -> None:
        # Publish first so the owning process sees the request even if this
        # process is terminated before it can perform a local cancellation.
        await self.registry.request_stop(stream_id)

        async with self._lock:
            task = self._tasks.get(stream_id)
            if not task:
                logger.info("stop requested for non-local stream %s", stream_id)
                return
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def list_local_streams(self) -> dict[str, str]:
        """Return mapping of local stream_id -> task_name for this instance."""
        async with self._lock:
            return {sid: t.get_name() for sid, t in self._tasks.items()}

    async def is_running_locally(self, stream_id: str) -> bool:
        async with self._lock:
            return stream_id in self._tasks

    async def stop_all(self) -> None:
        async with self._lock:
            stream_ids = list(self._tasks.keys())

        for stream_id in stream_ids:
            try:
                await self.stop_stream(stream_id)
            except Exception:
                logger.exception("error stopping stream %s", stream_id)

        logger.info("all streams stopped")
