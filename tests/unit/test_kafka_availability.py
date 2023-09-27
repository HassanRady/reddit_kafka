import os
import pytest

from kafka import KafkaProducer, KafkaConsumer


def test_kafka_producer_connection():
    try:
        producer = KafkaProducer(bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS"))
        assert producer.bootstrap_connected()  
        producer.close()
    except Exception as e:
        pytest.fail(f"Failed to connect to Kafka producer: {e}")

def test_kafka_consumer_connection():
    try:
        consumer = KafkaConsumer('test_topic', bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS"))
        assert consumer.bootstrap_connected() 
        consumer.close()
    except Exception as e:
        pytest.fail(f"Failed to connect to Kafka consumer: {e}")