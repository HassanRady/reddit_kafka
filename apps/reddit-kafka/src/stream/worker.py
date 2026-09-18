"""Cancel-safe stream worker that wraps the streaming loop."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

import asyncpraw
from asyncprawcore.exceptions import (
    Forbidden,
    NotFound,
    RequestException,
    TooManyRequests,
)
from opentelemetry import context as otel_context
from opentelemetry.trace import SpanKind, Status, StatusCode

from src.config import SchemaSettings
from src.models import RedditCommentMessage
from src.observability import METRICS, get_tracer, kafka_trace_headers
from src.repositories.stream_registry import StreamRegistry
from src.serializers.avro_serializer import get_serializer
from src.stream.circuit_breaker import CircuitBreaker
from src.stream.error_handler import ErrorHandler, RecoveryStrategy
from src.stream.exceptions import KafkaDeliveryError, LockLostError
from src.stream.lock import DistributedLockManager

logger = logging.getLogger(__name__)


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
        self._delivery_errors: list[str] = []
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

            # Construction may involve schema-registry I/O. Verify the exact
            # lease again before this worker is allowed to consume or produce.
            await self._refresh_lock_or_raise()

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
                    # manager leaves the stream eligible for failover.
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

                METRICS.reddit_comments_received.inc()

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
            METRICS.comments.labels(outcome="replayed").inc()
            logger.debug(f"Skipping already-processed comment {comment.id}")
            return

        started = time.perf_counter()
        outcome = "error"
        tracer = get_tracer("stream_worker")
        # A stream task can outlive the request that created it. Each comment is
        # therefore a new trace root, which is propagated to downstream consumers.
        with tracer.start_as_current_span(
            "reddit.comment.process",
            context=otel_context.Context(),
            kind=SpanKind.PRODUCER,
            attributes={
                "stream.id": self.stream_id,
                "stream.subreddit": self.subreddit,
                "messaging.system": "kafka",
                "messaging.destination.name": self.kafka_topic,
            },
        ) as span:
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

                try:
                    produce_options: dict[str, Any] = {
                        "key": self.subreddit.encode("utf-8"),
                        "on_delivery": self._on_delivery,
                    }
                    trace_headers = kafka_trace_headers()
                    if trace_headers:
                        produce_options["headers"] = trace_headers
                    self.kafka_producer.produce(
                        self.kafka_topic,
                        serialized_message,
                        **produce_options,
                    )
                    self.kafka_producer.poll(0)
                except Exception as e:
                    raise KafkaDeliveryError(
                        f"Kafka rejected comment {comment.id}: {e}"
                    ) from e

                self.comments_since_checkpoint += 1
                if self.comments_since_checkpoint >= self.checkpoint_interval:
                    await self._flush_delivery_batch()
                    await self._save_checkpoint_for_comment_id(str(comment.id))
                    self.comments_since_checkpoint = 0
                outcome = "queued"
                METRICS.comments.labels(outcome=outcome).inc()
            except asyncio.CancelledError:
                outcome = "cancelled"
                span.set_status(Status(StatusCode.ERROR, "processing cancelled"))
                raise
            except KafkaDeliveryError as error:
                outcome = "delivery_error"
                METRICS.comments.labels(outcome=outcome).inc()
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR))
                raise
            except Exception as error:
                outcome = "invalid"
                METRICS.comments.labels(outcome=outcome).inc()
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR))
                logger.exception(f"Error processing comment {comment.id}: {error}")
                await self.error_handler.record_error(
                    self.stream_id,
                    "CommentProcessingError",
                    str(error),
                    is_recoverable=True,
                )
            finally:
                METRICS.comment_processing_duration.labels(outcome=outcome).observe(
                    time.perf_counter() - started
                )

    def _on_delivery(self, error: Any, message: Any) -> None:
        """Record asynchronous delivery failures reported by librdkafka."""
        payload = message.value() if message is not None else None
        payload_size = (
            len(payload) if isinstance(payload, (bytes, bytearray, memoryview)) else 0
        )
        if error is None:
            METRICS.kafka_deliveries.labels(result="success").inc()
            METRICS.kafka_delivery_bytes.labels(result="success").inc(payload_size)
            METRICS.last_delivery_timestamp.set_to_current_time()
            return

        METRICS.kafka_deliveries.labels(result="error").inc()
        METRICS.kafka_delivery_bytes.labels(result="error").inc(payload_size)
        error_message = str(error)
        self._delivery_errors.append(error_message)
        logger.error(
            "Kafka delivery failed for stream %s: %s",
            self.stream_id,
            error_message,
        )

    async def _flush_delivery_batch(self) -> None:
        """Wait for batch delivery and fail before advancing its checkpoint."""
        try:
            remaining_messages = await asyncio.to_thread(self.kafka_producer.flush)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise KafkaDeliveryError(f"Kafka flush failed: {e}") from e

        delivery_errors = self._delivery_errors
        self._delivery_errors = []

        if remaining_messages:
            raise KafkaDeliveryError(
                f"Kafka flush left {remaining_messages} message(s) queued"
            )
        if delivery_errors:
            raise KafkaDeliveryError(
                f"Kafka failed to deliver {len(delivery_errors)} message(s): "
                f"{delivery_errors[0]}"
            )

    async def _handle_fetch_exception(self, error: Exception) -> None:
        if isinstance(error, TooManyRequests):
            logger.error("Rate limited by Reddit API")
        elif isinstance(error, (NotFound, Forbidden)):
            logger.error("Subreddit unavailable: %s", error)
            await self.registry.update_status_if_owner(
                self.stream_id, "error", self.instance_id
            )
        elif isinstance(error, RequestException):
            logger.error(f"Reddit API request error: {error}")
        else:
            logger.exception(f"Unexpected error in fetch_and_process: {error}")

        raise error

    async def _save_checkpoint_for_comment_id(self, comment_id: str) -> None:
        """Save checkpoint after processing a comment."""
        try:
            await self.registry.set_checkpoint(
                self.stream_id,
                last_comment_id=comment_id,
                last_processed_at=datetime.now(UTC).replace(tzinfo=None).isoformat(),
            )
            logger.debug(f"Checkpoint saved: {comment_id}")
        except Exception as e:
            logger.error(f"Error saving checkpoint: {e}")

    async def _save_checkpoint(self) -> None:
        """Save current checkpoint (called on shutdown)."""
        checkpoint = await self.registry.get_checkpoint(self.stream_id)
        last_comment_id = checkpoint.get("last_comment_id")
        if last_comment_id:
            await self._save_checkpoint_for_comment_id(last_comment_id)

    async def _lock_refresh_loop(self) -> None:
        """Refresh the lock, failing closed if ownership cannot be verified."""
        while True:
            await asyncio.sleep(self.lock_refresh_interval)
            await self._refresh_lock_or_raise()

    async def _refresh_lock_or_raise(self) -> None:
        """Refresh the exact lease token or fail before processing data."""
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
        # reflects liveness while the worker is running. Failures here do not
        # affect the Redis lease and therefore are non-fatal.
        try:
            await self.registry.heartbeat(self.stream_id, self.instance_id)
        except Exception:
            logger.debug(
                "Failed to update registry heartbeat for %s",
                self.stream_id,
            )

    def _record_lock_loss(self) -> None:
        """Record lock loss without delaying the watchdog failure."""
        logger.error("Lost or could not verify lock for %s", self.subreddit)
        METRICS.stream_lifecycle.labels(transition="lease", result="lost").inc()
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
            await self.registry.update_status_if_owner(
                self.stream_id, "error", self.instance_id
            )
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
