"""Deterministic external-service adapters for containerized E2E tests.

The application, Redis, PostgreSQL, Kafka producer, worker lifecycle, and HTTP API
are production code. Only Reddit and AWS Glue are replaced because they are
external, credentialed services unsuitable for deterministic CI.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import src.app as app_module
import src.stream.worker as worker_module


@dataclass(frozen=True)
class FakeAuthor:
    name: str


@dataclass(frozen=True)
class FakeComment:
    id: str
    body: str
    author: FakeAuthor


class FakeCommentStream:
    async def comments(self, *, skip_existing: bool) -> Any:
        del skip_existing
        sequence = 0
        while True:
            sequence += 1
            yield FakeComment(
                id=f"e2e-comment-{sequence:08d}",
                body=f"deterministic E2E comment {sequence}",
                author=FakeAuthor(name="e2e-author"),
            )
            await asyncio.sleep(0.005)


class FakeSubreddit:
    def __init__(self) -> None:
        self.stream = FakeCommentStream()

    async def load(self) -> None:
        return None


class FakeReddit:
    async def subreddit(self, name: str) -> FakeSubreddit:
        if not name:
            raise ValueError("subreddit is required")
        return FakeSubreddit()


class JsonTestSerializer:
    """Expose Kafka payloads to the E2E consumer without requiring AWS Glue."""

    def serialize(self, message: Any) -> bytes:
        return json.dumps(message.model_dump(), sort_keys=True).encode("utf-8")


def get_fake_reddit_client(settings: Any) -> FakeReddit:
    del settings
    return FakeReddit()


def get_json_serializer(*, schema_settings: Any, topic_name: str) -> JsonTestSerializer:
    del schema_settings, topic_name
    return JsonTestSerializer()


app_module._get_reddit_client = get_fake_reddit_client  # type: ignore[assignment]
worker_module.get_serializer = get_json_serializer  # type: ignore[assignment]

app = app_module.app
