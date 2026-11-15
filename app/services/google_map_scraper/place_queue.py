import hashlib
import json
from typing import TypedDict

from redis import Redis
from redis.exceptions import RedisError

from app.services.buffer.client import BufferError
from app.services.google_map_scraper.place_query import PlaceQuery
from app.utils.errors import describe

from .google_map.geo import BoundingBox, GridCell
from .scrape.scrape_types import LeadRow, SettingsData


class PlaceTile(TypedDict):
    ID: str
    keyword: str
    name: str
    settings: SettingsData
    bounding_box: BoundingBox | None
    cell: GridCell | None
    attempts: int
    scrape_id: str | None


class DoneTile(TypedDict):
    kept: list[LeadRow]
    finished: int
    skipped: int
    blocked: bool
    rows: list[LeadRow]


def run_id(query: PlaceQuery) -> str:
    payload = json.dumps(query.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class PlaceTileQueue:
    """The tiles of one city run in Redis: pending, in flight, and done.

    A tile moves from pending to in flight when a worker takes it, and from in
    flight to done when it finishes. A tile that must run again goes back to the
    front of pending.
    """

    def __init__(self, redis: Redis, query: PlaceQuery, ttl_seconds: int):
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        run = run_id(query)
        self._pending_key = f"place_queue:{run}:pending"
        self._in_flight_key = f"place_queue:{run}:in_flight"
        self._done_key = f"place_queue:{run}:done"

    def exists(self) -> bool:
        try:
            return bool(
                self._redis.exists(
                    self._pending_key, self._in_flight_key, self._done_key
                )
            )
        except RedisError as error:
            raise BufferError(
                f"could not check place run: {describe(error)}"
            ) from error

    def clear(self) -> None:
        try:
            self._redis.delete(self._pending_key, self._in_flight_key, self._done_key)
        except RedisError as error:
            raise BufferError(
                f"could not clear place run: {describe(error)}"
            ) from error

    def push(self, tile: PlaceTile) -> None:
        self._add("rpush", tile, "enqueue place tile")

    def push_front(self, tile: PlaceTile) -> None:
        self._add("lpush", tile, "requeue place tile")

    def take(self) -> PlaceTile | None:
        """Move the first pending tile to in flight in one Redis step, or return None."""
        try:
            raw = self._redis.lmove(
                self._pending_key, self._in_flight_key, "LEFT", "RIGHT"
            )
            if raw is not None:
                self._redis.expire(self._in_flight_key, self._ttl_seconds)
        except RedisError as error:
            raise BufferError(
                f"could not take place tile: {describe(error)}"
            ) from error
        return None if raw is None else json.loads(raw)

    def mark_scrape(self, tile_id: str, scrape_id: str) -> None:
        """Store the scrape id on an in-flight tile."""
        index, _, tile = self._locate(tile_id)
        tile["scrape_id"] = scrape_id
        try:
            self._redis.lset(
                self._in_flight_key, index, json.dumps(tile, sort_keys=True)
            )
        except RedisError as error:
            raise BufferError(
                f"could not mark place tile: {describe(error)}"
            ) from error

    def finish(self, tile_id: str, done: DoneTile) -> None:
        """Remove an in-flight tile and append its result to the done list."""
        _, raw, _ = self._locate(tile_id)
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.lrem(self._in_flight_key, 1, raw)
            pipe.rpush(self._done_key, json.dumps(done, sort_keys=True))
            pipe.expire(self._done_key, self._ttl_seconds)
            pipe.expire(self._in_flight_key, self._ttl_seconds)
            pipe.execute()
        except RedisError as error:
            raise BufferError(
                f"could not finish place tile: {describe(error)}"
            ) from error

    def requeue(self, tile_id: str, tile: PlaceTile) -> None:
        """Take an in-flight tile back out and put ``tile`` at the front of pending."""
        _, raw, _ = self._locate(tile_id)
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.lrem(self._in_flight_key, 1, raw)
            pipe.lpush(self._pending_key, json.dumps(tile, sort_keys=True))
            pipe.expire(self._pending_key, self._ttl_seconds)
            pipe.expire(self._in_flight_key, self._ttl_seconds)
            pipe.execute()
        except RedisError as error:
            raise BufferError(
                f"could not requeue place tile: {describe(error)}"
            ) from error

    def in_flight(self) -> list[PlaceTile]:
        return self._read(self._in_flight_key, "in-flight place tiles")

    def pending(self) -> list[PlaceTile]:
        return self._read(self._pending_key, "pending place tiles")

    def done(self) -> list[DoneTile]:
        return self._read(self._done_key, "finished place tiles")

    def _add(self, end: str, tile: PlaceTile, what: str) -> None:
        try:
            pipe = self._redis.pipeline(transaction=True)
            getattr(pipe, end)(self._pending_key, json.dumps(tile, sort_keys=True))
            pipe.expire(self._pending_key, self._ttl_seconds)
            pipe.execute()
        except RedisError as error:
            raise BufferError(f"could not {what}: {describe(error)}") from error

    def _read(self, key: str, what: str) -> list:
        try:
            return [json.loads(raw) for raw in self._redis.lrange(key, 0, -1)]
        except RedisError as error:
            raise BufferError(f"could not read {what}: {describe(error)}") from error

    def _locate(self, tile_id: str) -> tuple[int, str, PlaceTile]:
        """The index, raw JSON and tile of an in-flight tile."""
        try:
            raws = self._redis.lrange(self._in_flight_key, 0, -1)
        except RedisError as error:
            raise BufferError(
                f"could not read in-flight place tiles: {describe(error)}"
            ) from error
        for index, raw in enumerate(raws):
            tile = json.loads(raw)
            if tile["ID"] == tile_id:
                return index, raw, tile
        raise BufferError(f"place tile {tile_id} is not in flight")
