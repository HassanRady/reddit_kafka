"""Cancel-safe stream worker that wraps the streaming loop."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import asyncpraw
from asyncprawcore.exceptions import (
    Forbidden,
    NotFound,
    RequestException,
    TooManyRequests,
)

from src.config import SchemaSettings
from src.models import RedditCommentMessage
from src.repositories.stream_registry import StreamRegistry
from src.serializers.avro_serializer import get_serializer
from src.stream.circuit_breaker import CircuitBreaker
from src.stream.error_handler import ErrorHandler, RecoveryStrategy
from src.stream.lock import DistributedLockManager

logger = logging.getLogger(__name__)


class LockLostError(RuntimeError):
    """Raised when a worker can no longer prove ownership of its lock."""


class StreamWorker:
    """
    Cancel-safe worker for streaming Reddit comments.

    Responsibilities:
    - Listen for CancelledError (graceful shutdown)
    - Use circuit breaker for rate limit handling
    - Track errors for debugging
    - Update checkpoints periodically
    - Support Ctrl+C and lifespan shutdown
    """

    def __init__(
        self,
        subreddit: str,
        reddit_client: asyncpraw.Reddit,
        kafka_producer: Any,
        registry: "StreamRegistry",
        lock_manager: "DistributedLockManager",
        instance_id: str,
        stream_id: str,
        kafka_topic: str,
        schema_settings: SchemaSettings,
        lock_token: str,
    ) -> None:
        """
        Args:
            subreddit: Subreddit name to stream
            reddit_client: PRAW Reddit client (async)
            kafka_producer: Kafka producer (confluent_kafka)
            registry: StreamRegistry for checkpoints
            lock_manager: DistributedLockManager for locks
            instance_id: Instance ID
            stream_id: Stream UUID
            kafka_topic: Kafka topic to produce to
            schema_settings: SchemaSettings for Avro serialization
            lock_token: Exact ownership token returned when the lease was acquired
        """
        self.subreddit = subreddit
        self.reddit_client = reddit_client
        self.kafka_producer = kafka_producer
        self.registry = registry
        self.lock_manager = lock_manager
        self.instance_id = instance_id
        self.stream_id = stream_id
        self.kafka_topic = kafka_topic
        self.schema_settings = schema_settings
        self.lock_token = lock_token

        self.circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=60,
        )
        self.error_handler = ErrorHandler(
            registry._redis, session_maker=getattr(registry, "_session_maker", None)
        )

        self.serializer = get_serializer(
            schema_settings=schema_settings, topic_name=self.kafka_topic
        )
        logger.info(
            f"Initialized Avro serializer (AWS: {schema_settings.use_localstack}, "
            f"Region: {schema_settings.aws_region})"
        )

        self.checkpoint_interval = 100
        self.comments_since_checkpoint = 0
        self.lock_refresh_interval = 30
        self.lock_ttl = 60
        self.lock_refresh_timeout = 10
        # Event set when the worker should stop due to lock loss or other
        # cooperative shutdown triggers. Observed by the main loop and
        # long-running streaming loops so the worker can shutdown cleanly.
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        """Run the streaming loop (cancel-safe).

        This is the main entry point. Handles:
        - Cancel signals gracefully
        - Errors with exponential backoff
        - Checkpoint saves
        - Lock refresh
        """
        shutdown_event = False
        stream_task: asyncio.Task[None] | None = None
        lock_task: asyncio.Task[None] | None = None

        try:
            logger.info(f"StreamWorker starting for {self.subreddit}")

            stream_task = asyncio.create_task(
                self._stream_loop(), name=f"stream-loop-{self.stream_id}"
            )
            lock_task = asyncio.create_task(
                self._lock_refresh_loop(), name=f"lock-refresh-{self.stream_id}"
            )

            try:
                done, _ = await asyncio.wait(
                    {stream_task, lock_task}, return_when=asyncio.FIRST_COMPLETED
                )

                if lock_task in done:
                    shutdown_event = True
                    # The refresher only completes by raising on lock loss or an
                    # inability to verify ownership. Propagate that failure so the
                    # manager records the stream as errored.
                    await lock_task
                    raise LockLostError(
                        f"Lock refresher stopped unexpectedly for {self.subreddit}"
                    )

                await stream_task
            except asyncio.CancelledError:
                logger.info(f"StreamWorker cancelled for {self.subreddit}")
                shutdown_event = True
                raise
            finally:
                for task in (stream_task, lock_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(stream_task, lock_task, return_exceptions=True)

        finally:
            if shutdown_event:
                try:
                    await self._save_checkpoint()
                    logger.info(f"✓ Saved final checkpoint for {self.subreddit}")
                except Exception as e:
                    logger.error(f"Error saving final checkpoint: {e}")

            try:
                released = await self.lock_manager.release_lock(
                    self.subreddit, self.lock_token
                )
                if released:
                    logger.info(f"✓ Released lock for {self.subreddit}")
            except Exception as e:
                logger.error(f"Error releasing lock: {e}")

            logger.info(f"StreamWorker finished for {self.subreddit}")

    async def _stream_loop(self) -> None:
        """Process the stream until cancelled or a fatal stream error occurs."""
        while True:
            if self._stop_event.is_set():
                logger.info("Stop event set for %s, exiting main loop", self.subreddit)
                return
            try:
                await self.circuit_breaker.call(self._fetch_and_process_comments)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._handle_error(e)

    async def _fetch_and_process_comments(self) -> None:
        """Fetch comments from Reddit and produce to Kafka.

        Called periodically by circuit breaker. Handles one batch of comments
        or one streaming session until backoff is needed.
        """
        subreddit = await self.reddit_client.subreddit(self.subreddit)

        checkpoint = await self.registry.get_checkpoint(self.stream_id)
        skip_existing = checkpoint.get("last_comment_id") is None

        try:
            async for comment in subreddit.stream.comments(skip_existing=skip_existing):
                # Cooperative stop requested while streaming
                if self._stop_event.is_set():
                    logger.info(
                        "Stop event set during streaming for %s, breaking fetch loop",
                        self.subreddit,
                    )
                    break

                # Check for cancellation
                await asyncio.sleep(0)  # Yield control to allow cancellation

                if comment is None:
                    continue

                # A yielded comment proves that the long-lived Reddit stream is
                # healthy. Report progress now because this coroutine normally
                # runs forever and CircuitBreaker.call() cannot wait for it to
                # return before clearing transient failure history.
                await self.circuit_breaker.record_success()
                await self._process_comment(comment, checkpoint)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._handle_fetch_exception(e)

    async def _process_comment(self, comment: Any, checkpoint: dict[str, Any]) -> None:
        if (
            checkpoint.get("last_comment_id")
            and comment.id == checkpoint["last_comment_id"]
        ):
            logger.debug(f"Skipping already-processed comment {comment.id}")
            return

        try:
            message = RedditCommentMessage(
                subreddit=self.subreddit,
                author_id=comment.author.name if comment.author else "[deleted]",
                text=comment.body,
                timestamp=datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
            )

            serialized_message = self.serializer.serialize(message)
            if serialized_message is None:
                raise ValueError("Serializer returned no payload")

            self.kafka_producer.produce(
                self.kafka_topic,
                serialized_message,
            )
            self.kafka_producer.poll(0)

            # Update checkpoint every N comments
            self.comments_since_checkpoint += 1
            if self.comments_since_checkpoint >= self.checkpoint_interval:
                await self._save_checkpoint_for_comment(comment)
                await asyncio.to_thread(self.kafka_producer.flush)
                self.comments_since_checkpoint = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Error processing comment {comment.id}: {e}")
            await self.error_handler.record_error(
                self.stream_id,
                "CommentProcessingError",
                str(e),
                is_recoverable=True,
            )

    async def _handle_fetch_exception(self, error: Exception) -> None:
        if isinstance(error, TooManyRequests):
            logger.error("Rate limited by Reddit API")
        elif isinstance(error, (NotFound, Forbidden)):
            logger.error("Subreddit unavailable: %s", error)
            await self.registry.update_status(self.stream_id, "error")
        elif isinstance(error, RequestException):
            logger.error(f"Reddit API request error: {error}")
        else:
            logger.exception(f"Unexpected error in fetch_and_process: {error}")

        raise error

    async def _save_checkpoint_for_comment(self, comment: Any) -> None:
        """Save checkpoint after processing a comment."""
        try:
            await self.registry.set_checkpoint(
                self.stream_id,
                last_comment_id=comment.id,
                last_processed_at=datetime.now(UTC).replace(tzinfo=None).isoformat(),
            )
            logger.debug(f"Checkpoint saved: {comment.id}")
        except Exception as e:
            logger.error(f"Error saving checkpoint: {e}")

    async def _save_checkpoint(self) -> None:
        """Save current checkpoint (called on shutdown)."""
        checkpoint = await self.registry.get_checkpoint(self.stream_id)
        if checkpoint.get("last_comment_id"):
            await self._save_checkpoint_for_comment(
                type("", (), {"id": checkpoint["last_comment_id"]})()
            )

    async def _lock_refresh_loop(self) -> None:
        """Refresh the lock, failing closed if ownership cannot be verified."""
        while True:
            await asyncio.sleep(self.lock_refresh_interval)
            try:
                success = await asyncio.wait_for(
                    self.lock_manager.refresh_lock(
                        self.subreddit,
                        self.lock_token,
                        ttl=self.lock_ttl,
                    ),
                    timeout=self.lock_refresh_timeout,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_lock_loss()
                raise LockLostError(
                    f"Could not verify lock ownership for {self.subreddit}"
                ) from exc

            if not success:
                self._record_lock_loss()
                raise LockLostError(f"Lock ownership lost for {self.subreddit}")

            # Send a lightweight heartbeat to the registry so `updated_at`
            # reflects liveness while the worker is running. Failures here do
            # not affect the Redis lease and therefore are non-fatal.
            try:
                await self.registry.update_status(
                    self.stream_id, "active", instance_id=self.instance_id
                )
            except Exception:
                logger.debug(
                    "Failed to update registry heartbeat for %s",
                    self.stream_id,
                )

    def _record_lock_loss(self) -> None:
        """Record lock loss without delaying the watchdog failure."""
        logger.error("Lost or could not verify lock for %s", self.subreddit)
        self._stop_event.set()

    async def _handle_error(self, error: Exception) -> None:
        """Handle error and decide on recovery strategy."""
        error_type = error.__class__.__name__

        await self.error_handler.record_error(
            self.stream_id,
            error_type,
            str(error),
            is_recoverable=ErrorHandler.is_retryable(error),
        )

        if RecoveryStrategy.should_abandon_stream(error):
            logger.error(f"Fatal error, abandoning stream: {error}")
            await self.registry.update_status(self.stream_id, "error")
            raise error

        if RecoveryStrategy.should_retry_with_backoff(error):
            backoff = ErrorHandler.get_backoff_duration(error)
            logger.warning(f"Rate limited, backing off for {backoff}s")
            await asyncio.sleep(backoff)

        elif RecoveryStrategy.should_retry_immediately(error):
            logger.warning(f"Retryable error, retrying immediately: {error}")

        else:
            logger.error(f"Unhandled error: {error}")
            raise error
