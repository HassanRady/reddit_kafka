"""StreamManager that starts/stops per-subreddit streaming tasks."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from src.observability import METRICS, get_tracer
from src.repositories.stream_registry import StreamNotFoundError, StreamRegistry
from src.stream.exceptions import LockLostError
from src.stream.lock import DistributedLockManager

logger = logging.getLogger(__name__)

LEASE_TTL_SECONDS = 60


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
        self._reconcile_task: asyncio.Task[None] | None = None
        self._reconcile_interval = 10.0
        self._shutting_down = False

    async def start_stream(
        self, subreddit: str, config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create registry entry, acquire lock, and start runner task for subreddit.

        Raises StreamExistsError if a stream for the subreddit already exists.
        Raises RuntimeError if lock cannot be acquired.
        Returns the stream metadata dict from the registry.
        """
        tracer = get_tracer("stream_manager")
        with tracer.start_as_current_span(
            "stream.start",
            attributes={"stream.subreddit": subreddit},
        ) as span:
            meta = await self.registry.create_stream(
                subreddit, config=config, instance_id=self.instance_id
            )
            span.set_attribute("stream.id", str(meta["id"]))
        stream_id = meta["id"]

        # Try to acquire distributed lock
        lock_token: str | None = None
        if self.lock_manager:
            lock_token = await self.lock_manager.acquire_lock(
                subreddit, self.instance_id, ttl=LEASE_TTL_SECONDS
            )
            if lock_token is None:
                # Delete only the exact unclaimed snapshot. A reconciler or
                # another owner may have changed it after creation.
                await self.registry.delete_stream(
                    stream_id,
                    expected_status="starting",
                    expected_updated_at=str(meta.get("updated_at") or ""),
                )
                METRICS.stream_lifecycle.labels(
                    transition="start", result="lock_contended"
                ).inc()
                raise RuntimeError(f"Cannot acquire lock for subreddit {subreddit}")

        try:
            started = await self._start_local_worker(
                stream_id, subreddit, lock_token, transition="start"
            )
        except Exception:
            if self.lock_manager and lock_token:
                await self.lock_manager.release_lock(subreddit, lock_token)
            raise
        if not started:
            if self.lock_manager and lock_token:
                await self.lock_manager.release_lock(subreddit, lock_token)
            raise RuntimeError(
                f"Stream {stream_id} could not be activated safely; "
                "lease ownership was lost or this instance is shutting down"
            )

        return meta

    async def _start_local_worker(
        self,
        stream_id: str,
        subreddit: str,
        lock_token: str | None,
        *,
        transition: str,
    ) -> bool:
        """Claim registry ownership and create one local worker task."""
        async with self._lock:
            if self._shutting_down or stream_id in self._tasks:
                return False

            if (
                self.lock_manager
                and lock_token
                and not await self.lock_manager.owns_lock(subreddit, lock_token)
            ):
                logger.warning(
                    "lease expired before activation for stream %s", stream_id
                )
                return False

            await self.registry.update_status(
                stream_id, "active", instance_id=self.instance_id
            )
            # PostgreSQL persistence in update_status may outlive the lease. Do
            # not start a worker unless the exact token still owns it afterward.
            if (
                self.lock_manager
                and lock_token
                and not await self.lock_manager.owns_lock(subreddit, lock_token)
            ):
                logger.warning("lease expired while activating stream %s", stream_id)
                return False

            task = asyncio.create_task(
                self._run(stream_id, subreddit, lock_token),
                name=f"stream-{stream_id}",
                # Workers can run for days. Do not retain a request span or the
                # reconciler context for their complete lifetime.
                context=contextvars.Context(),
            )
            self._tasks[stream_id] = task

        METRICS.active_streams.inc()
        METRICS.stream_lifecycle.labels(transition=transition, result="success").inc()
        return True

    async def resume_stream(self, meta: dict[str, Any]) -> bool:
        """Try to adopt an existing stream whose previous owner disappeared."""
        if self.lock_manager is None or self._shutting_down:
            return False

        stream_id = str(meta.get("id") or "")
        subreddit = str(meta.get("subreddit") or "")
        if not stream_id or not subreddit:
            return False

        async with self._lock:
            if stream_id in self._tasks:
                return False

        if await self.lock_manager.is_locked(subreddit):
            return False

        lock_token = await self.lock_manager.acquire_lock(
            subreddit, self.instance_id, ttl=LEASE_TTL_SECONDS
        )
        if lock_token is None:
            METRICS.stream_lifecycle.labels(
                transition="reconcile", result="lock_contended"
            ).inc()
            return False

        try:
            # Re-read after acquiring the lease. A stop request may have raced
            # with the reconciliation scan.
            current = await self.registry.get_stream(stream_id)
            if current.get("status") not in {"active", "starting"}:
                return False
            if await self.registry.is_stop_requested(stream_id):
                return False

            started = await self._start_local_worker(
                stream_id, subreddit, lock_token, transition="reconcile"
            )
            if started:
                logger.info(
                    "adopted orphaned stream %s (subreddit=%s)",
                    stream_id,
                    subreddit,
                )
                return True
            return False
        finally:
            if not await self.is_running_locally(stream_id):
                await self.lock_manager.release_lock(subreddit, lock_token)

    async def reconcile_once(self) -> None:
        """Adopt runnable streams and finish stops abandoned by dead owners."""
        streams = await self.registry.list_streams()
        for meta in streams:
            stream_id = str(meta.get("id") or "")
            subreddit = str(meta.get("subreddit") or "")
            status = meta.get("status")
            if not stream_id or not subreddit:
                continue

            if self._needs_reconciliation(meta):
                try:
                    await self.resume_stream(meta)
                except Exception:
                    METRICS.stream_lifecycle.labels(
                        transition="reconcile", result="error"
                    ).inc()
                    logger.exception("failed to reconcile stream %s", stream_id)
            elif status == "stopping" and meta.get("updated_at"):
                try:
                    finalized = await self.registry.finalize_stop_if_unowned(
                        stream_id,
                        subreddit,
                        str(meta["updated_at"]),
                    )
                    if finalized:
                        logger.info("finalized orphaned stop for stream %s", stream_id)
                except Exception:
                    logger.exception(
                        "failed to finalize orphaned stop for stream %s", stream_id
                    )

    @staticmethod
    def _needs_reconciliation(meta: dict[str, Any]) -> bool:
        status = meta.get("status")
        if status == "active":
            return True
        if status != "starting" or not meta.get("updated_at"):
            return False

        try:
            updated_at = datetime.fromisoformat(
                str(meta["updated_at"]).replace("Z", "+00:00")
            )
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
        except ValueError:
            logger.warning(
                "cannot parse starting stream timestamp: %s", meta["updated_at"]
            )
            return False
        return (datetime.now(UTC) - updated_at).total_seconds() >= LEASE_TTL_SECONDS

    async def start_reconciliation(self, interval: float = 10.0) -> None:
        """Start periodic distributed stream reconciliation."""
        if self._reconcile_task and not self._reconcile_task.done():
            return
        self._reconcile_interval = interval
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name=f"stream-reconciler-{self.instance_id}"
        )
        logger.info("stream reconciliation started (interval=%ss)", interval)

    async def stop_reconciliation(self) -> None:
        task = self._reconcile_task
        self._reconcile_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        logger.info("stream reconciliation stopped")

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile_once()
                await asyncio.sleep(self._reconcile_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stream reconciliation sweep failed")
                await asyncio.sleep(self._reconcile_interval)

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
            await self.registry.update_status_if_owner(
                stream_id, "stopped", self.instance_id
            )
            METRICS.stream_lifecycle.labels(transition="stop", result="completed").inc()
            logger.info("runner finished for %s (id=%s)", subreddit, stream_id)
        except asyncio.CancelledError:
            # graceful cancellation
            runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await runner_task
            await self._handle_runner_cancellation(stream_id, subreddit)
            raise
        except LockLostError:
            # Losing a lease is an ownership transition, not a terminal stream
            # failure. Leave the registry runnable so a reconciler can adopt it.
            METRICS.stream_lifecycle.labels(
                transition="lease", result="relinquished"
            ).inc()
            logger.warning(
                "runner lost ownership and is awaiting failover: %s (id=%s)",
                subreddit,
                stream_id,
            )
        except StreamNotFoundError:
            METRICS.stream_lifecycle.labels(
                transition="run", result="stream_not_found"
            ).inc()
            logger.exception("stream not found in registry during run: %s", stream_id)
        except Exception:
            await self.registry.update_status_if_owner(
                stream_id, "error", self.instance_id
            )
            METRICS.stream_lifecycle.labels(transition="run", result="error").inc()
            logger.exception("stream %s crashed", stream_id)
        finally:
            if not runner_task.done():
                runner_task.cancel()
                with suppress(asyncio.CancelledError):
                    await runner_task
            stop_watcher.cancel()
            with suppress(asyncio.CancelledError):
                await stop_watcher
            removed_local_task = False
            async with self._lock:
                if stream_id in self._tasks:
                    del self._tasks[stream_id]
                    removed_local_task = True
            if removed_local_task:
                METRICS.active_streams.dec()

    async def _handle_runner_cancellation(self, stream_id: str, subreddit: str) -> None:
        preserve_for_failover = await self._should_preserve_for_failover(stream_id)
        if preserve_for_failover:
            METRICS.stream_lifecycle.labels(transition="drain", result="released").inc()
            logger.info("runner drained for failover: %s (id=%s)", subreddit, stream_id)
            return

        await self.registry.update_status_if_owner(
            stream_id, "stopped", self.instance_id
        )
        METRICS.stream_lifecycle.labels(transition="stop", result="cancelled").inc()
        logger.info("runner cancelled for %s (id=%s)", subreddit, stream_id)

    async def _should_preserve_for_failover(self, stream_id: str) -> bool:
        if not self._shutting_down:
            return False
        try:
            return not await self.registry.is_stop_requested(stream_id)
        except Exception:
            logger.exception(
                "could not inspect stop request for stream %s during shutdown",
                stream_id,
            )
            return True

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
        with get_tracer("stream_manager").start_as_current_span(
            "stream.stop",
            attributes={"stream.id": stream_id},
        ):
            await self._stop_stream(stream_id)

    async def _stop_stream(self, stream_id: str) -> None:
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
        """Drain local workers while leaving them eligible for another replica."""
        self._shutting_down = True
        await self.stop_reconciliation()

        async with self._lock:
            tasks = list(self._tasks.values())

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("all local streams drained for failover")
