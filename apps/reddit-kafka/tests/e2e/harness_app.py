"""Deterministic external-service adapters for containerized E2E tests.

The application, Redis, PostgreSQL, Kafka producer, worker lifecycle, and HTTP API
are production code. Only Reddit and AWS Glue are replaced because they are
external, credentialed services unsuitable for deterministic CI.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from asyncprawcore.exceptions import Forbidden, NotFound, RequestException

import src.app as app_module
import src.stream.worker as worker_module


@dataclass(frozen=True)
class FakeAuthor:
    name: str


@dataclass(frozen=True)
class FakeComment:
    id: str
    body: str
    author: FakeAuthor | None


@dataclass(frozen=True)
class FakeResponse:
    status: int


class FakeCommentStream:
    def __init__(self, subreddit: str, reddit: "FakeReddit") -> None:
        self.subreddit = subreddit
        self.reddit = reddit

    async def comments(self, *, skip_existing: bool) -> Any:
        del skip_existing
        self.reddit.stream_attempts[self.subreddit] = (
            self.reddit.stream_attempts.get(self.subreddit, 0) + 1
        )
        if (
            self.subreddit == "e2e_transient_failure"
            and self.reddit.stream_attempts[self.subreddit] == 1
        ):
            raise RequestException(ConnectionError("temporary outage"), (), {})
        if self.subreddit == "e2e_fatal_failure":
            raise ValueError("deterministic fatal stream failure")

        sequence = 0
        while True:
            sequence += 1
            author: FakeAuthor | None = FakeAuthor(name="e2e-author")
            body = f"deterministic E2E comment {sequence}"
            if self.subreddit == "e2e_comment_edges" and sequence == 1:
                author = None
            elif self.subreddit == "e2e_comment_edges" and sequence == 2:
                body = ""
            yield FakeComment(
                id=f"e2e-comment-{sequence:08d}",
                body=body,
                author=author,
            )
            await asyncio.sleep(0.005)


class FakeSubreddit:
    def __init__(self, name: str, reddit: "FakeReddit") -> None:
        self.name = name
        self.stream = FakeCommentStream(name, reddit)

    async def load(self) -> None:
        if self.name == "e2e_missing":
            raise NotFound(FakeResponse(status=404))  # type: ignore[arg-type]
        if self.name == "e2e_forbidden":
            raise Forbidden(FakeResponse(status=403))  # type: ignore[arg-type]
        if self.name == "e2e_validation_outage":
            raise ConnectionError("deterministic validation outage")
        return None


class FakeReddit:
    def __init__(self) -> None:
        self.stream_attempts: dict[str, int] = {}

    async def subreddit(self, name: str) -> FakeSubreddit:
        if not name:
            raise ValueError("subreddit is required")
        return FakeSubreddit(name, self)

    async def close(self) -> None:
        return None


class JsonTestSerializer:
    """Expose Kafka payloads to the E2E consumer without requiring AWS Glue."""

    def serialize(self, message: Any) -> bytes:
        return json.dumps(message.model_dump(), sort_keys=True).encode("utf-8")


def get_fake_reddit_client(settings: Any) -> FakeReddit:
    del settings
    return FakeReddit()


class FastE2EStreamWorker(worker_module.StreamWorker):
    """Production worker with shorter observation intervals for deterministic E2E."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.checkpoint_interval = 5
        self.lock_refresh_interval = 0.2
        self.lock_refresh_timeout = 1


def get_json_serializer(*, schema_settings: Any) -> JsonTestSerializer:
    del schema_settings
    return JsonTestSerializer()


app_module._get_reddit_client = get_fake_reddit_client  # type: ignore[assignment]
app_module.StreamWorker = FastE2EStreamWorker
worker_module.get_serializer = get_json_serializer  # type: ignore[assignment]

app = app_module.app
