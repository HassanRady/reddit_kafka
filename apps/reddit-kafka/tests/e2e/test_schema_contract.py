import json
from pathlib import Path

from src.models import RedditCommentMessage

PROJECT_ROOT = Path(__file__).parents[2]


def test_model_example_and_avro_schema_have_the_same_fields() -> None:
    schema = json.loads((PROJECT_ROOT / "schemas/reddit_comment.avsc").read_text())
    example = json.loads((PROJECT_ROOT / "schemas/example_message.json").read_text())

    model_fields = set(RedditCommentMessage.model_fields)
    avro_fields = {field["name"] for field in schema["fields"]}

    assert avro_fields == model_fields
    assert set(example) == model_fields
    assert RedditCommentMessage.model_validate(example).model_dump() == example
