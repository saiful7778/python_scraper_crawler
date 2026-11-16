"""Small Redis list reads shared by the buffer queues."""

import json
from typing import Any

from redis import Redis


def read_records(redis: Redis, key: str) -> list[dict[str, Any]]:
    """Every JSON record of a list, in list order."""
    return [json.loads(raw) for raw in redis.lrange(key, 0, -1)]


def sum_lengths(redis: Redis, *keys: str) -> int:
    """The combined length of several lists, read in one round trip."""
    pipe = redis.pipeline()
    for key in keys:
        pipe.llen(key)
    return sum(pipe.execute())
