import os
import pytest
from kafka import KafkaProducer, KafkaConsumer
from src.reddit_kafka_pipeline import stream_pipeline_client as _stream_pipeline_client


@pytest.fixture(scope="session")
def stream_pipeline_client():
    return _stream_pipeline_client

@pytest.fixture(scope="session")
def producer():
    return KafkaProducer(bootstrap_servers=os.environ["KAFKA_BOOTSTRAP_SERVERS"])

@pytest.fixture(scope="session")
def kafka_consumer():
    return KafkaConsumer(bootstrap_servers=os.environ["KAFKA_BOOTSTRAP_SERVERS"])