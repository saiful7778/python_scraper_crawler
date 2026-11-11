import json
from typing import TypedDict

from redis import Redis

from app.services.buffer.keys import (
    active_key,
    processing_key,
    queue_key,
    queued_key,
)


class WebsiteJob(TypedDict):
    scrape_id: str
    position: int
    row_id: str
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


class WebsiteQueue:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._enqueue = redis.register_script(_ENQUEUE)
        self._requeue_processing = redis.register_script(_REQUEUE_PROCESSING)

    def enqueue(self, scrape_id: str, job: WebsiteJob) -> bool:
        added = self._enqueue(
            keys=[queued_key(scrape_id), queue_key(scrape_id)],
            args=[self._ttl_seconds, job["origin"], json.dumps(job)],
        )
        return bool(added)

    def claim(self, scrape_id: str) -> WebsiteJob | None:
        pipe = self._redis.pipeline()
        pipe.lmove(queue_key(scrape_id), processing_key(scrape_id), "LEFT", "RIGHT")
        pipe.expire(processing_key(scrape_id), self._ttl_seconds)
        pipe.expire(queue_key(scrape_id), self._ttl_seconds)
        raw = pipe.execute()[0]
        return None if raw is None else json.loads(raw)

    def complete(self, scrape_id: str, job: WebsiteJob) -> None:
        pipe = self._redis.pipeline()
        pipe.lrem(processing_key(scrape_id), 1, json.dumps(job))
        pipe.expire(processing_key(scrape_id), self._ttl_seconds)
        pipe.execute()

    def requeue_processing(self, scrape_id: str) -> int:
        return self._requeue_processing(
            keys=[processing_key(scrape_id), queue_key(scrape_id)],
            args=[self._ttl_seconds],
        )

    def pending_jobs(self, scrape_id: str) -> list[WebsiteJob]:
        """Every job still queued or claimed, without taking them out."""
        return [*self.processing_jobs(scrape_id), *self.queued_jobs(scrape_id)]

    def processing_jobs(self, scrape_id: str) -> list[WebsiteJob]:
        """The jobs claimed by a crawler thread and not yet completed."""
        return [
            json.loads(raw)
            for raw in self._redis.lrange(processing_key(scrape_id), 0, -1)
        ]

    def queued_jobs(self, scrape_id: str) -> list[WebsiteJob]:
        """The jobs no crawler thread has claimed yet."""
        return [
            json.loads(raw) for raw in self._redis.lrange(queue_key(scrape_id), 0, -1)
        ]

    def pending(self, scrape_id: str) -> int:
        pipe = self._redis.pipeline()
        pipe.llen(queue_key(scrape_id))
        pipe.llen(processing_key(scrape_id))
        return sum(pipe.execute())

    def discard(self, scrape_id: str) -> None:
        self._redis.delete(
            queue_key(scrape_id), processing_key(scrape_id), queued_key(scrape_id)
        )

    def scrape_ids_with_work(self) -> list[str]:
        return [
            scrape_id
            for scrape_id in self._redis.smembers(active_key())
            if self.pending(scrape_id) > 0
        ]
