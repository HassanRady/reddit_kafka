def _test_pipeline(stream_pipeline_client, kafka_consumer):
    subreddit = "memes"

    stream_pipeline_client.stream_subreddit(subreddit)

    kafka_consumer.subscribe(topics=[stream_pipeline_client.kafka_topic_name])
    kafka_consumer.poll(timeout_ms=5000)

    fetched_data = next(kafka_consumer)

    assert fetched_data is not None
    assert fetched_data.value is not None
    assert fetched_data.value["subreddit"] == subreddit
    assert isinstance(fetched_data.value, dict)
    assert isinstance(fetched_data.value["title"], str)
