import redis.asyncio as redis

from src.config import RedisSettings

_redis: redis.Redis | None = None


def get_redis(settings: RedisSettings) -> redis.Redis:
    """Return a singleton async Redis client configured from `settings`.

    Uses simple module-level caching so multiple imports return the same client.
    """
    global _redis

    if _redis is None:
        _redis = redis.Redis(
            host=settings.host,
            port=settings.port,
            username=settings.user,
            password=settings.password.get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            ssl=settings.use_ssl,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        try:
            await _redis.close()
            await _redis.connection_pool.disconnect()
        except Exception:
            pass
        _redis = None
