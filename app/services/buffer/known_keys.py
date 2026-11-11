from collections.abc import Iterable
from itertools import batched

from redis import Redis
from redis.exceptions import OutOfMemoryError

from app.services.buffer.client import FULL_MESSAGE, BufferFullError
from app.services.buffer.keys import known_key
from app.services.google_map_scraper.scrape.dedupe import lead_keys
from app.services.google_map_scraper.scrape.scrape_types import LeadRow

LOAD_BATCH_SIZE = 1000

_ADD_IF_NEW = """
for i = 2, #ARGV do
    if redis.call('SISMEMBER', KEYS[1], ARGV[i]) == 1 then
        return 0
    end
end
redis.call('SADD', KEYS[1], unpack(ARGV, 2))
redis.call('EXPIRE', KEYS[1], ARGV[1])
return 1
"""


class KnownKeys:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._add_if_new = redis.register_script(_ADD_IF_NEW)

    def load(self, scrape_id: str, keys: Iterable[str]) -> int:
        key = known_key(scrape_id)
        try:
            for batch in batched(keys, LOAD_BATCH_SIZE):
                self._redis.sadd(key, *batch)
        except OutOfMemoryError as exc:
            raise BufferFullError(FULL_MESSAGE) from exc
        self._redis.expire(key, self._ttl_seconds)
        return self._redis.scard(key)

    def add_if_new(self, scrape_id: str, row: LeadRow) -> bool:
        keys = lead_keys(row)
        if not keys:
            return True
        try:
            added = self._add_if_new(
                keys=[known_key(scrape_id)], args=[self._ttl_seconds, *keys]
            )
        except OutOfMemoryError as exc:
            raise BufferFullError(FULL_MESSAGE) from exc
        return bool(added)

    def forget(self, scrape_id: str, row: LeadRow) -> None:
        keys = lead_keys(row)
        if keys:
            self._redis.srem(known_key(scrape_id), *keys)

    def discard(self, scrape_id: str) -> None:
        self._redis.delete(known_key(scrape_id))
