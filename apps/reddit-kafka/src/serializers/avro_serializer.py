"""Kafka message serialization using AWS Glue Schema Registry and Avro."""

import logging
from functools import lru_cache
from pathlib import Path
from typing import cast
from uuid import UUID

import boto3
from aws_schema_registry.avro import AvroSchema
from aws_schema_registry.codec import encode
from botocore.exceptions import ClientError, NoCredentialsError
from pydantic import BaseModel, ValidationError

from src.config import SchemaSettings
from src.models import RedditCommentMessage

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas/reddit_comment.avsc"
)


class GlueAvroMessageSerializer[T: BaseModel]:
    """Serialize Kafka messages with Avro and AWS Glue Schema Registry."""

    def __init__(
        self,
        registry_name: str,
        schema_name: str,
        schema_version: int,
        aws_region: str,
    ):
        self.schema_name = schema_name

        logger.info("Initializing AWS Glue client in %s", aws_region)
        self.glue_client = boto3.client("glue", region_name=aws_region)

        try:
            schema_version_response = self.glue_client.get_schema_version(
                SchemaId={"RegistryName": registry_name, "SchemaName": schema_name},
                SchemaVersionNumber={"VersionNumber": schema_version},
            )
            if schema_version_response.get("Status") != "AVAILABLE":
                raise ValueError(
                    "Schema version is not available: "
                    f"{schema_version_response.get('Status', 'UNKNOWN')}"
                )
            self.avro_schema = AvroSchema(schema_version_response["SchemaDefinition"])
            self.schema_version_id = UUID(schema_version_response["SchemaVersionId"])
            logger.info("Loaded Avro schema for %s from AWS Glue", schema_name)
        except (ClientError, KeyError, NoCredentialsError, ValueError) as error:
            logger.error(
                "Failed to load schema %s/%s: %s", registry_name, schema_name, error
            )
            raise RuntimeError(
                "Cannot start serializer without schema access."
            ) from error

    def serialize(self, message: T) -> bytes:
        """Serialize and frame a message using the cached Glue schema version ID."""
        try:
            avro_payload = cast(
                bytes,
                self.avro_schema.write(message.model_dump()),
            )
            return cast(bytes, encode(avro_payload, self.schema_version_id))
        except ValidationError as error:
            logger.error(
                "Pydantic validation failed for %s:\n%s", self.schema_name, error
            )
            raise ValueError(
                f"Invalid message format for schema {self.schema_name}"
            ) from error
        except Exception as error:
            logger.error("AWS Glue serialization failed: %s", error)
            raise


class LocalAvroMessageSerializer[T: BaseModel]:
    """Serialize Avro messages using the checked-in schema without AWS."""

    def __init__(self, schema_path: Path = DEFAULT_LOCAL_SCHEMA_PATH) -> None:
        self.schema_path = schema_path
        self.avro_schema = AvroSchema(schema_path.read_text())
        logger.info("Loaded local Avro schema from %s", schema_path)

    def serialize(self, message: T) -> bytes:
        """Serialize a Pydantic model as schemaless Avro bytes."""
        return cast(bytes, self.avro_schema.write(message.model_dump()))


@lru_cache(maxsize=4)
def _get_cached_serializer(
    registry_name: str,
    schema_name: str,
    schema_version: int,
    aws_region: str,
) -> GlueAvroMessageSerializer[RedditCommentMessage]:
    return GlueAvroMessageSerializer(
        registry_name=registry_name,
        schema_name=schema_name,
        schema_version=schema_version,
        aws_region=aws_region,
    )


@lru_cache(maxsize=1)
def _get_cached_local_serializer() -> LocalAvroMessageSerializer[RedditCommentMessage]:
    return LocalAvroMessageSerializer()


def get_serializer(
    schema_settings: SchemaSettings,
) -> (
    GlueAvroMessageSerializer[RedditCommentMessage]
    | LocalAvroMessageSerializer[RedditCommentMessage]
):
    """Get or create the message serializer singleton."""
    if not schema_settings.use_aws_schema_registry:
        return _get_cached_local_serializer()

    return _get_cached_serializer(
        registry_name=schema_settings.registry_name,
        schema_name=schema_settings.schema_name,
        schema_version=schema_settings.schema_version,
        aws_region=schema_settings.aws_region,
    )


__all__ = ["GlueAvroMessageSerializer", "LocalAvroMessageSerializer", "get_serializer"]
