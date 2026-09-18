import pytest
from pydantic import ValidationError

from src.models import RedditCommentMessage


def make_message(**overrides: str) -> RedditCommentMessage:
    values = {
        "subreddit": "python",
        "author_id": "example_user",
        "text": "A comment body",
        "timestamp": "2026-05-09T14:30:45.123456Z",
    }
    values.update(overrides)
    return RedditCommentMessage.model_validate(values)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-05-09Z",
        "2026-05-09 14:30:45Z",
        "2026-05-09T14:30:45+02:00Z",
        "2026-05-09T14:30:45ZZ",
        "2026-02-30T14:30:45Z",
    ],
)
def test_rejects_invalid_utc_timestamps(timestamp: str) -> None:
    with pytest.raises(ValidationError):
        make_message(timestamp=timestamp)


def test_accepts_utc_timestamp_without_fractional_seconds() -> None:
    message = make_message(timestamp="2026-05-09T14:30:45Z")

    assert message.timestamp == "2026-05-09T14:30:45Z"


def test_rejects_unknown_event_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RedditCommentMessage.model_validate(
            {
                "subreddit": "python",
                "author_id": "example_user",
                "text": "A comment body",
                "timestamp": "2026-05-09T14:30:45Z",
                "unexpected": "value",
            }
        )
