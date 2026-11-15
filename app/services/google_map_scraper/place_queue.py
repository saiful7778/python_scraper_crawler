import hashlib
import json
from typing import TypedDict

from redis import Redis
from redis.exceptions import RedisError

from app.services.buffer.client import BufferError
from app.services.google_map_scraper.place_query import PlaceQuery
from app.utils.errors import describe


class PlaceJob(TypedDict):
    keyword: str
    name: str
    settings: dict
    bounding_box: list[float] | None
    cells: list[dict] | None
    next_cell: int
    rows: list[dict]
    kept: list[dict]
    finished: int
    skipped: int


class DoneKeyword(TypedDict):
    kept: list[dict]
    finished: int
    skipped: int
    blocked: bool


def run_id(query: PlaceQuery) -> str:
    payload = json.dumps(query.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class PlaceJobQueue:
    def __init__(self, redis: Redis, query: PlaceQuery, ttl_seconds: int):
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        run = run_id(query)
        self._jobs_key = f"place_queue:{run}:jobs"
        self._done_key = f"place_queue:{run}:done"

    def exists(self) -> bool:
        try:
            return bool(self._redis.exists(self._jobs_key, self._done_key))
        except RedisError as error:
            raise BufferError(f"could not check place run: {describe(error)}") from error

    def enqueue(self, job: PlaceJob) -> None:
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.rpush(self._jobs_key, json.dumps(job, sort_keys=True))
            pipe.expire(self._jobs_key, self._ttl_seconds)
            pipe.execute()
        except RedisError as error:
            raise BufferError(f"could not enqueue place job: {describe(error)}") from error

    def peek(self) -> PlaceJob | None:
        try:
            raw = self._redis.lindex(self._jobs_key, 0)
        except RedisError as error:
            raise BufferError(f"could not read place job: {describe(error)}") from error
        return None if raw is None else json.loads(raw)

    def save(self, job: PlaceJob) -> None:
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.lset(self._jobs_key, 0, json.dumps(job, sort_keys=True))
            pipe.expire(self._jobs_key, self._ttl_seconds)
            pipe.expire(self._done_key, self._ttl_seconds)
            pipe.execute()
        except RedisError as error:
            raise BufferError(f"could not save place job: {describe(error)}") from error

    def complete(self, done: DoneKeyword) -> None:
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.rpush(self._done_key, json.dumps(done, sort_keys=True))
            pipe.lpop(self._jobs_key)
            pipe.expire(self._done_key, self._ttl_seconds)
            pipe.expire(self._jobs_key, self._ttl_seconds)
            pipe.execute()
        except RedisError as error:
            raise BufferError(f"could not complete place job: {describe(error)}") from error

    def done(self) -> list[DoneKeyword]:
        try:
            return [json.loads(raw) for raw in self._redis.lrange(self._done_key, 0, -1)]
        except RedisError as error:
            raise BufferError(f"could not read finished place jobs: {describe(error)}") from error

    def clear(self) -> None:
        try:
            self._redis.delete(self._jobs_key, self._done_key)
        except RedisError as error:
            raise BufferError(f"could not clear place run: {describe(error)}") from error
