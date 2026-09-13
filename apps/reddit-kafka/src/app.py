import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import asyncpraw
from asyncprawcore.exceptions import Forbidden, NotFound
from confluent_kafka import Producer
from fastapi import FastAPI, HTTPException

from src.config import Settings
from src.db import close_db, get_engine, get_session, init_db
from src.redis_client import close_redis, get_redis
from src.repositories.stream_registry import (
    StreamExistsError,
    StreamNotFoundError,
    StreamRegistry,
)
from src.stream.lock import DistributedLockManager
from src.stream.manager import StreamManager
from src.stream.worker import StreamWorker
from src.tasks import CheckpointFlusher
from src.tasks.dead_stream_cleanup import DeadStreamCleanup

logger = logging.getLogger(__name__.split(".")[0])
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())


_reddit_client: asyncpraw.Reddit | None = None
_kafka_producer: Producer | None = None


def _get_reddit_client(settings: Settings) -> asyncpraw.Reddit:
    """Get or create Reddit client."""
    global _reddit_client
    if _reddit_client is None:
        _reddit_client = asyncpraw.Reddit(
            client_id=settings.reddit.client_id,
            client_secret=settings.reddit.client_secret.get_secret_value(),
            user_agent=settings.reddit.user_agent,
        )
    return _reddit_client


def _get_kafka_producer(config: dict[str, Any]) -> Producer:
    global _kafka_producer
    if _kafka_producer is None:
        _kafka_producer = Producer(config)
    return _kafka_producer


def _build_kafka_producer_config(settings: Settings) -> dict[str, Any]:
    config: dict[str, Any] = {"bootstrap.servers": settings.kafka.bootstrap_servers}

    if settings.kafka.security_protocol != "PLAINTEXT":
        config["security.protocol"] = settings.kafka.security_protocol

    if settings.kafka.sasl_username and settings.kafka.sasl_password is not None:
        config["sasl.mechanisms"] = settings.kafka.sasl_mechanism
        config["sasl.username"] = settings.kafka.sasl_username
        config["sasl.password"] = settings.kafka.sasl_password.get_secret_value()

    if settings.kafka.ssl_ca_location:
        config["ssl.ca.location"] = settings.kafka.ssl_ca_location

    return config


async def _create_runner(
    registry: StreamRegistry,
    lock_manager: DistributedLockManager,
    instance_id: str,
    settings: Settings,
) -> Any:
    """Create a runner function that uses StreamWorker."""
    reddit_client = _get_reddit_client(settings)
    kafka_producer = _get_kafka_producer_from_settings(settings)
    kafka_topic = settings.kafka.raw_text_topic
    schema_settings = settings.schema_settings

    async def runner(subreddit: str, lock_token: str | None) -> None:
        """Runner that creates a StreamWorker and runs it."""
        if lock_token is None:
            raise RuntimeError(f"No lock token supplied for subreddit {subreddit}")

        stream_meta = await registry.get_stream_by_subreddit(subreddit)
        if not stream_meta:
            raise RuntimeError(f"Stream not found for subreddit {subreddit}")

        stream_id = stream_meta["id"]

        worker = StreamWorker(
            subreddit=subreddit,
            reddit_client=reddit_client,
            kafka_producer=kafka_producer,
            registry=registry,
            lock_manager=lock_manager,
            instance_id=instance_id,
            stream_id=stream_id,
            kafka_topic=kafka_topic,
            schema_settings=schema_settings,
            lock_token=lock_token,
        )

        try:
            await worker.run()
        except asyncio.CancelledError:
            logger.info(f"Runner cancelled for {subreddit}")
            raise

    return runner


def _get_kafka_producer_from_settings(settings: Settings) -> Producer:
    return _get_kafka_producer(_build_kafka_producer_config(settings))


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    logger.info("Starting up...")
    settings = Settings()

    await init_db(settings.postgres)
    logger.info("✓ Postgres initialized")

    redis = get_redis(settings.redis)
    _ = await redis.ping()  # type: ignore
    logger.info("✓ Redis connected")

    # Provide DB session maker to registry and other components so they can persist
    session_maker = get_session

    app.state.registry = StreamRegistry(redis, session_maker=session_maker)
    app.state.lock_manager = DistributedLockManager(redis)
    app.state.instance_id = str(uuid.uuid4())[:8]

    # Reddit client singleton for validation and worker creation
    app.state.reddit_client = _get_reddit_client(settings)

    app.state.runner = await _create_runner(
        app.state.registry,
        app.state.lock_manager,
        app.state.instance_id,
        settings,
    )

    app.state.manager = StreamManager(
        app.state.registry,
        app.state.runner,
        app.state.instance_id,
        lock_manager=app.state.lock_manager,
    )
    logger.info(f"✓ Manager initialized (instance_id={app.state.instance_id})")

    engine = get_engine()
    if engine is None:
        raise RuntimeError("Engine not initialized after init_db")

    app.state.flusher = CheckpointFlusher(
        redis,
        session_maker,  # type: ignore
        flush_interval=settings.db_flush_interval,
    )
    await app.state.flusher.start()
    logger.info("✓ Checkpoint flusher started")

    app.state.cleanup = DeadStreamCleanup(
        app.state.registry,
        redis,
        cleanup_interval=settings.dead_stream_cleanup_interval,
    )
    await app.state.cleanup.start()
    logger.info("✓ Dead stream cleanup started")

    yield

    logger.info("Shutting down...")

    if hasattr(app.state, "cleanup"):
        await app.state.cleanup.stop()
        logger.info("✓ Dead stream cleanup stopped")

    # First stop local stream workers so they can perform their shutdown
    # logic (save checkpoints to Redis, release locks, etc.). After
    # workers have stopped, immediately flush Redis checkpoints to Postgres
    # so we don't lose the final state.
    if hasattr(app.state, "manager"):
        await app.state.manager.stop_all()
        logger.info("✓ All streams stopped")

    # Flush any remaining checkpoints to Postgres before stopping the flusher
    if hasattr(app.state, "flusher"):
        try:
            await app.state.flusher.flush()
        except Exception:
            logger.exception("Error during final checkpoint flush")
        await app.state.flusher.stop()
        logger.info("✓ Checkpoint flusher stopped")

    await close_redis()
    logger.info("✓ Redis closed")

    await close_db()
    logger.info("✓ Postgres closed")

    logger.info("Shutdown complete")


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/streams")
async def create_stream(subreddit: str) -> dict[str, Any]:
    """Start streaming a subreddit."""
    # Pre-validate subreddit availability to avoid creating registry entries
    reddit = getattr(app.state, "reddit_client", None)
    if reddit is None:
        raise HTTPException(status_code=503, detail="Reddit client not available")

    try:
        sr = await reddit.subreddit(subreddit)
        await sr.load()
    except NotFound:
        raise HTTPException(
            status_code=404, detail=f"Subreddit '{subreddit}' not found"
        ) from None
    except Forbidden:
        raise HTTPException(
            status_code=403, detail=f"Subreddit '{subreddit}' is private or forbidden"
        ) from None
    except Exception as e:
        logger.exception("Error validating subreddit %s: %s", subreddit, e)
        raise HTTPException(
            status_code=503, detail="Error validating subreddit"
        ) from None

    try:
        meta = await app.state.manager.start_stream(subreddit)
        return dict(meta)
    except StreamExistsError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None


@app.get("/streams")
async def list_streams() -> list[dict[str, Any]]:
    """List all streams."""
    result = await app.state.registry.list_streams()
    return list(result)


@app.post("/streams/{stream_id}/stop")
async def stop_stream(stream_id: str) -> dict[str, Any]:
    """Stop a stream."""
    try:
        await app.state.manager.stop_stream(stream_id)
    except StreamNotFoundError:
        raise HTTPException(status_code=404, detail="Stream not found") from None
    return {"stream_id": stream_id, "status": "stopping"}
