import json
from typing import Any, cast

from redis import Redis

from app.services.buffer.client import BufferError
from app.services.buffer.keys import (
    active_key,
    known_key,
    processing_key,
    queue_key,
    queued_key,
    rows_key,
    state_key,
)
from app.services.google_map_scraper.scrape.scrape_types import (
    ProgressData,
    SettingsData,
)

_CREATE = """
redis.call('HSET', KEYS[1], 'settings', ARGV[1], 'status', 'pending')
redis.call('SADD', KEYS[2], ARGV[2])
for i = 1, #KEYS do
    redis.call('EXPIRE', KEYS[i], ARGV[3])
end
return 1
"""

_WRITE = """
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
for i = 1, #KEYS do
    redis.call('EXPIRE', KEYS[i], ARGV[3])
end
return 1
"""


def _scrape_keys(scrape_id: str) -> list[str]:
    return [
        state_key(scrape_id),
        rows_key(scrape_id),
        known_key(scrape_id),
        queue_key(scrape_id),
        processing_key(scrape_id),
        queued_key(scrape_id),
    ]


def _load(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BufferError("Buffered scrape state is not valid JSON") from exc


class ScrapeState:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._create = redis.register_script(_CREATE)
        self._write = redis.register_script(_WRITE)

    def create(self, scrape_id: str, settings_data: SettingsData) -> None:
        self._create(
            keys=[state_key(scrape_id), active_key(), *_scrape_keys(scrape_id)[1:]],
            args=[json.dumps(settings_data), scrape_id, self._ttl_seconds],
        )

    def get_settings(self, scrape_id: str) -> SettingsData | None:
        raw = self._redis.hget(state_key(scrape_id), "settings")
        return None if raw is None else _load(cast(str, raw))

    def save_progress(self, scrape_id: str, progress: ProgressData) -> None:
        self._write(
            keys=_scrape_keys(scrape_id),
            args=["progress", json.dumps(progress), self._ttl_seconds],
        )

    def get_progress(self, scrape_id: str) -> ProgressData:
        raw = self._redis.hget(state_key(scrape_id), "progress")
        return {} if raw is None else _load(cast(str, raw))

    def set_status(self, scrape_id: str, status: str) -> None:
        self._write(
            keys=_scrape_keys(scrape_id),
            args=["status", status, self._ttl_seconds],
        )

    def discard(self, scrape_id: str) -> None:
        pipe = self._redis.pipeline()
        pipe.delete(*_scrape_keys(scrape_id))
        pipe.srem(active_key(), scrape_id)
        pipe.execute()

    def active_ids(self) -> list[str]:
        return cast(list[str], list(self._redis.smembers(active_key())))

    def refresh_ttl(self, scrape_id: str) -> None:
        pipe = self._redis.pipeline()
        for key in _scrape_keys(scrape_id):
            pipe.expire(key, self._ttl_seconds)
        pipe.execute()
