import re
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


class RedditCommentMessage(BaseModel):
    """Pydantic model for Reddit comment messages published to Kafka."""

    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        populate_by_name=True,
    )

    subreddit: str = Field(
        ..., description="Subreddit name (e.g., 'python', 'MachineLearning')"
    )
    author_id: str = Field(
        ..., description="Username of the comment author. '[deleted]' if deleted"
    )
    text: str = Field(..., description="The comment body text", min_length=1)
    timestamp: str = Field(
        ..., description="ISO-8601 timestamp with Z suffix (UTC timezone)"
    )

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        if not UTC_TIMESTAMP_PATTERN.fullmatch(value):
            raise ValueError(
                "Timestamp must use ISO-8601 UTC format 'YYYY-MM-DDTHH:MM:SS[.ffffff]Z'"
            )

        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"Invalid ISO-8601 timestamp format: {error}") from error

        if parsed.tzinfo != UTC:
            raise ValueError("Timestamp must be in UTC")

        return value

    @field_validator("subreddit")
    @classmethod
    def validate_subreddit(cls, value: str) -> str:
        if len(value) == 0:
            raise ValueError("Subreddit cannot be empty")
        if len(value) > 255:
            raise ValueError("Subreddit name too long (max 255 characters)")
        return value

    @field_validator("author_id")
    @classmethod
    def validate_author_id(cls, value: str) -> str:
        if len(value) == 0:
            raise ValueError("Author ID cannot be empty")
        if len(value) > 255:
            raise ValueError("Author ID too long (max 255 characters)")
        return value


__all__ = ["RedditCommentMessage"]
