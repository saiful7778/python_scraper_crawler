import redis

from app.utils.errors import ScraperError
from config.redis import RedisConfig


class BufferError(ScraperError):
    """The Redis buffer failed to store or return data."""


class BufferFullError(BufferError):
    """Redis is out of memory and cannot accept more buffered data."""


FULL_MESSAGE = (
    "Redis memory limit reached (maxmemory 200mb in infra/redis/redis.conf); "
    "raise it to buffer more leads"
)


def connect(config: RedisConfig) -> redis.Redis:
    return redis.Redis.from_url(config.url, decode_responses=True)
