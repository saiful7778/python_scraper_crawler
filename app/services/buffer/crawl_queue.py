import json
from typing import TypedDict

from redis import Redis

from app.services.buffer.keys import (
    crawl_processing_key,
    crawl_queue_key,
    crawl_queued_key,
)


class CrawlJob(TypedDict):
    website: str
    origin: str


_ENQUEUE = """
if redis.call('SADD', KEYS[1], ARGV[2]) == 0 then
    return 0
end
redis.call('RPUSH', KEYS[2], ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[1])
return 1
"""

_COMPLETE = """
local removed = redis.call('LREM', KEYS[1], 1, ARGV[2])
if removed > 0 then
    redis.call('SREM', KEYS[2], ARGV[3])
end
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[1])
return removed
"""

_REQUEUE_PROCESSING = """
local moved = 0
while redis.call('LMOVE', KEYS[1], KEYS[2], 'RIGHT', 'LEFT') do
    moved = moved + 1
end
if moved > 0 then
    redis.call('EXPIRE', KEYS[2], ARGV[1])
end
return moved
"""


def _encode(job: CrawlJob) -> str:
    return json.dumps(job, sort_keys=True)


class CrawlQueue:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._enqueue = redis.register_script(_ENQUEUE)
        self._complete = redis.register_script(_COMPLETE)
        self._requeue_processing = redis.register_script(_REQUEUE_PROCESSING)

    def enqueue(self, job: CrawlJob) -> bool:
        added = self._enqueue(
            keys=[crawl_queued_key(), crawl_queue_key()],
            args=[self._ttl_seconds, job["origin"], _encode(job)],
        )
        return bool(added)

    def claim(self) -> CrawlJob | None:
        pipe = self._redis.pipeline()
        pipe.lmove(crawl_queue_key(), crawl_processing_key(), "LEFT", "RIGHT")
        pipe.expire(crawl_processing_key(), self._ttl_seconds)
        pipe.expire(crawl_queue_key(), self._ttl_seconds)
        pipe.expire(crawl_queued_key(), self._ttl_seconds)
        raw = pipe.execute()[0]
        return None if raw is None else json.loads(raw)

    def complete(self, job: CrawlJob) -> None:
        self._complete(
            keys=[crawl_processing_key(), crawl_queued_key()],
            args=[self._ttl_seconds, _encode(job), job["origin"]],
        )

    def requeue_processing(self) -> int:
        return self._requeue_processing(
            keys=[crawl_processing_key(), crawl_queue_key()],
            args=[self._ttl_seconds],
        )

    def queued_jobs(self) -> list[CrawlJob]:
        return [json.loads(raw) for raw in self._redis.lrange(crawl_queue_key(), 0, -1)]

    def pending(self) -> int:
        pipe = self._redis.pipeline()
        pipe.llen(crawl_queue_key())
        pipe.llen(crawl_processing_key())
        return sum(pipe.execute())
