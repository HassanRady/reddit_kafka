from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from aws_schema_registry.codec import decode

import src.serializers.avro_serializer as serializer_module
from src.config import SchemaSettings
from src.models import RedditCommentMessage
from src.serializers.avro_serializer import (
    GlueAvroMessageSerializer,
    LocalAvroMessageSerializer,
    get_serializer,
)

PROJECT_ROOT = Path(__file__).parents[2]
SCHEMA_VERSION_ID = UUID("12345678-1234-5678-1234-567812345678")


def test_serializer_fetches_schema_once_and_reuses_version_id_for_every_event(
    monkeypatch,
) -> None:
    glue_client = MagicMock()
    glue_client.get_schema_version.return_value = {
        "SchemaDefinition": (PROJECT_ROOT / "schemas/reddit_comment.avsc").read_text(),
        "SchemaVersionId": str(SCHEMA_VERSION_ID),
        "Status": "AVAILABLE",
    }

    monkeypatch.setattr(
        serializer_module.boto3,
        "client",
        MagicMock(return_value=glue_client),
    )
    serializer = GlueAvroMessageSerializer[RedditCommentMessage](
        registry_name="reddit-kafka-schemas",
        schema_name="RedditComment",
        schema_version=3,
        aws_region="us-east-1",
    )

    glue_client.get_schema_version.assert_called_once_with(
        SchemaId={
            "RegistryName": "reddit-kafka-schemas",
            "SchemaName": "RedditComment",
        },
        SchemaVersionNumber={"VersionNumber": 3},
    )

    message = RedditCommentMessage(
        subreddit="python",
        author_id="example_user",
        text="A comment body",
        timestamp="2026-05-09T14:30:45.123456Z",
    )
    first_payload = serializer.serialize(message)
    second_payload = serializer.serialize(message)

    for payload in (first_payload, second_payload):
        avro_payload, schema_version_id = decode(payload)
        assert schema_version_id == SCHEMA_VERSION_ID
        assert serializer.avro_schema.read(avro_payload) == message.model_dump()
    glue_client.get_schema_version.assert_called_once()
    assert len(glue_client.method_calls) == 1


def test_serializer_rejects_an_unavailable_schema_version(monkeypatch) -> None:
    glue_client = MagicMock()
    glue_client.get_schema_version.return_value = {"Status": "PENDING"}
    monkeypatch.setattr(
        serializer_module.boto3,
        "client",
        MagicMock(return_value=glue_client),
    )

    with pytest.raises(RuntimeError, match="without schema access"):
        GlueAvroMessageSerializer[RedditCommentMessage](
            registry_name="reddit-kafka-schemas",
            schema_name="RedditComment",
            schema_version=3,
            aws_region="us-east-1",
        )


def test_local_serializer_does_not_create_an_aws_client(
    monkeypatch,
) -> None:
    monkeypatch.setenv("USE_AWS_SCHEMA_REGISTRY", "false")
    monkeypatch.setattr(
        serializer_module.boto3,
        "client",
        MagicMock(side_effect=AssertionError("AWS client must not be created")),
    )
    serializer_module._get_cached_local_serializer.cache_clear()

    serializer = get_serializer(SchemaSettings())

    assert isinstance(serializer, LocalAvroMessageSerializer)
    message = RedditCommentMessage(
        subreddit="python",
        author_id="example_user",
        text="A comment body",
        timestamp="2026-05-09T14:30:45Z",
    )
    payload = serializer.serialize(message)
    assert serializer.avro_schema.read(payload) == message.model_dump()
