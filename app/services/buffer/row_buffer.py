import json
from typing import cast

from redis import Redis
from redis.exceptions import OutOfMemoryError

from app.services.buffer.client import FULL_MESSAGE, BufferFullError
from app.services.buffer.keys import active_key, rows_key
from app.services.google_map_scraper.scrape.scrape_types import LeadRow


class RowBuffer:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    def append(self, scrape_id: str, rows: list[LeadRow]) -> None:
        if not rows:
            return
        key = rows_key(scrape_id)
        pipe = self._redis.pipeline()
        pipe.rpush(key, *(json.dumps(row) for row in rows))
        pipe.expire(key, self._ttl_seconds)
        pipe.sadd(active_key(), scrape_id)
        pipe.expire(active_key(), self._ttl_seconds)
        try:
            pipe.execute()
        except OutOfMemoryError as exc:
            raise BufferFullError(FULL_MESSAGE) from exc

    def peek(self, scrape_id: str, limit: int) -> list[LeadRow]:
        if limit <= 0:
            return []
        return self._range(scrape_id, limit - 1)

    def trim(self, scrape_id: str, count: int) -> None:
        self._redis.ltrim(rows_key(scrape_id), count, -1)

    def length(self, scrape_id: str) -> int:
        return self._redis.llen(rows_key(scrape_id))

    def discard(self, scrape_id: str) -> None:
        self._redis.delete(rows_key(scrape_id))

    def read_all(self, scrape_id: str) -> list[LeadRow]:
        return self._range(scrape_id, -1)

    def _range(self, scrape_id: str, stop: int) -> list[LeadRow]:
        raw_rows = self._redis.lrange(rows_key(scrape_id), 0, stop)
        return [cast(LeadRow, json.loads(raw)) for raw in raw_rows]
