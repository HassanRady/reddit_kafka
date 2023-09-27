import asyncio
import json
import asyncpraw
from asyncprawcore.exceptions import RequestException, TooManyRequests
from confluent_kafka import Producer

from src.logger import get_file_logger
from src.config import settings

logger = get_file_logger(logger_name=__name__, file_name="logs")

def get_reddit_client():
    return asyncpraw.Reddit(
        client_id=settings.REDDIT_CLIENT_ID,
        client_secret=settings.REDDIT_CLIENT_SECRET,
        user_agent=settings.REDDIT_USER_AGENT,
        username=settings.REDDIT_USERNAME,
        password=settings.REDDIT_PASSWORD,
    )

def get_kafka_producer():
    config = {
        "bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS,
    }
    return Producer(config)

def value_serializer(value):
    return json.dumps(value).encode('utf-8')


reddit_api_client = get_reddit_client()
producer = get_kafka_producer()
running = True
async def stream_subreddit_producer_loop(subreddit_name):
    while running:
        try:
            subreddit = await reddit_api_client.subreddit(subreddit_name)
            async for comment in subreddit.stream.comments(skip_existing=True):
                if comment is None:
                    continue

                value = {"subreddit": subreddit_name,
                        "author_id": comment.author.name,
                        "text": comment.body, }
                producer.produce(settings.KAFKA_RAW_TEXT_TOPIC, value_serializer(value))
        except RequestException as e:
            logger.info("sleeping for 10 seconds")
            await asyncio.sleep(10)
        except TooManyRequests as e:
            logger.info("Too many requests, sleeping for 60 seconds")            
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(e)
            raise e

