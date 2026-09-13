from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RedditCommentMessage(BaseModel):
    """Pydantic model for Reddit comment messages published to Kafka."""

    model_config = ConfigDict(use_enum_values=True, populate_by_name=True)

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
        if not value.endswith("Z"):
            raise ValueError("Timestamp must end with 'Z' (UTC timezone indicator)")

        try:
            datetime.fromisoformat(value.rstrip("Z"))
        except ValueError as error:
            raise ValueError(f"Invalid ISO-8601 timestamp format: {error}") from error

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
