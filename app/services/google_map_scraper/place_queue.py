import hashlib
import json
from typing import NotRequired, TypedDict

from redis import Redis
from redis.commands.core import Script
from redis.exceptions import RedisError

from app.services.buffer.client import BufferError
from app.services.google_map_scraper.place_query import PlaceQuery
from app.utils.errors import describe

from .google_map.geo import BoundingBox, GridCell
from .scrape.scrape_types import LeadRow, SettingsData


class PlaceTile(TypedDict):
    ID: str
    keyword: str
    keyword_index: int
    cell_index: int | None
    name: str
    settings: SettingsData
    bounding_box: BoundingBox | None
    cell: GridCell | None
    attempts: int
    scrape_id: str | None
    kept: NotRequired[
        list[LeadRow]
    ]  # rows kept by earlier attempts of a re-queued tile
    rows: NotRequired[list[LeadRow]]  # deduped rows of those earlier attempts


class DoneTile(TypedDict):
    keyword_index: int
    kept: list[LeadRow]
    finished: int
    skipped: int
    blocked: bool
    rows: list[LeadRow]


_TAKE = """
local raw = redis.call('LPOP', KEYS[1])
if not raw then
    return false
end
local id = cjson.decode(raw)['ID']
redis.call('RPUSH', KEYS[2], id)
redis.call('HSET', KEYS[3], id, raw)
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[1])
redis.call('EXPIRE', KEYS[3], ARGV[1])
return raw
"""

_MARK_SCRAPE = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then
    return 0
end
local tile = cjson.decode(raw)
tile['scrape_id'] = ARGV[2]
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(tile))
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""

_FINISH = """
if redis.call('HEXISTS', KEYS[2], ARGV[2]) == 0 then
    return 0
end
redis.call('HDEL', KEYS[2], ARGV[2])
redis.call('LREM', KEYS[1], 1, ARGV[2])
redis.call('RPUSH', KEYS[3], ARGV[1])
redis.call('EXPIRE', KEYS[3], ARGV[3])
return 1
"""

_REQUEUE = """
if redis.call('HEXISTS', KEYS[2], ARGV[2]) == 0 then
    return 0
end
redis.call('HDEL', KEYS[2], ARGV[2])
redis.call('LREM', KEYS[1], 1, ARGV[2])
redis.call('LPUSH', KEYS[3], ARGV[1])
redis.call('EXPIRE', KEYS[3], ARGV[3])
return 1
"""

_IN_FLIGHT = """
local ids = redis.call('LRANGE', KEYS[1], 0, -1)
local tiles = {}
for index, id in ipairs(ids) do
    tiles[index] = redis.call('HGET', KEYS[2], id)
end
return tiles
"""


def run_id(query: PlaceQuery) -> str:
    payload = json.dumps(query.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class PlaceTileQueue:
    """The tiles of one city run in Redis: pending, in flight, and done.

    Pending and done are lists of tile JSON. In flight is a list of tile ids
    plus a hash of their JSON, so claiming, updating and finishing a tile touch
    one entry instead of reading and rewriting the whole in-flight list.
    """

    def __init__(self, redis: Redis, query: PlaceQuery, ttl_seconds: int):
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        run = run_id(query)
        self._pending_key = f"place_queue:{run}:pending"
        self._in_flight_key = f"place_queue:{run}:in_flight"
        self._in_flight_data_key = f"place_queue:{run}:in_flight_data"
        self._done_key = f"place_queue:{run}:done"
        self._take = redis.register_script(_TAKE)
        self._mark_scrape = redis.register_script(_MARK_SCRAPE)
        self._finish = redis.register_script(_FINISH)
        self._requeue = redis.register_script(_REQUEUE)
        self._in_flight = redis.register_script(_IN_FLIGHT)

    def exists(self) -> bool:
        try:
            return bool(
                self._redis.exists(
                    self._pending_key,
                    self._in_flight_key,
                    self._in_flight_data_key,
                    self._done_key,
                )
            )
        except RedisError as error:
            raise BufferError(
                f"could not check place run: {describe(error)}"
            ) from error

    def clear(self) -> None:
        try:
            self._redis.delete(
                self._pending_key,
                self._in_flight_key,
                self._in_flight_data_key,
                self._done_key,
            )
        except RedisError as error:
            raise BufferError(
                f"could not clear place run: {describe(error)}"
            ) from error

    def push(self, tile: PlaceTile) -> None:
        self._add("rpush", tile, "enqueue place tile")

    def push_front(self, tile: PlaceTile) -> None:
        self._add("lpush", tile, "requeue place tile")

    def insert_after_keyword(self, keyword_index: int, tiles: list[PlaceTile]) -> None:
        """Insert tiles after the last pending tile of ``keyword_index``.

        With no pending tile of that keyword, they go before the first pending tile
        of a later keyword, or at the end of pending.
        """
        try:
            raws = self._redis.lrange(self._pending_key, 0, -1)
            keywords = [json.loads(raw)["keyword_index"] for raw in raws]
            same = [i for i, index in enumerate(keywords) if index == keyword_index]
            later = [i for i, index in enumerate(keywords) if index > keyword_index]
            if same:
                position = same[-1] + 1
            else:
                position = later[0] if later else len(raws)
            new = (
                raws[:position]
                + [json.dumps(tile, sort_keys=True) for tile in tiles]
                + raws[position:]
            )
            pipe = self._redis.pipeline(transaction=True)
            pipe.delete(self._pending_key)
            pipe.rpush(self._pending_key, *new)
            pipe.expire(self._pending_key, self._ttl_seconds)
            pipe.execute()
        except RedisError as error:
            raise BufferError(
                f"could not insert place tiles: {describe(error)}"
            ) from error

    def take(self) -> PlaceTile | None:
        """Move the first pending tile to in flight in one Redis step, or return None."""
        raw = self._run(
            self._take,
            keys=[self._pending_key, self._in_flight_key, self._in_flight_data_key],
            args=[self._ttl_seconds],
            what="take place tile",
        )
        return None if not raw else json.loads(raw)

    def mark_scrape(self, tile_id: str, scrape_id: str) -> None:
        """Store the scrape id on an in-flight tile."""
        marked = self._run(
            self._mark_scrape,
            keys=[self._in_flight_data_key],
            args=[tile_id, scrape_id, self._ttl_seconds],
            what="mark place tile",
        )
        if not marked:
            raise BufferError(f"place tile {tile_id} is not in flight")

    def finish(self, tile_id: str, done: DoneTile) -> None:
        """Remove an in-flight tile and append its result to the done list."""
        finished = self._run(
            self._finish,
            keys=[self._in_flight_key, self._in_flight_data_key, self._done_key],
            args=[json.dumps(done, sort_keys=True), tile_id, self._ttl_seconds],
            what="finish place tile",
        )
        if not finished:
            raise BufferError(f"place tile {tile_id} is not in flight")

    def requeue(self, tile_id: str, tile: PlaceTile) -> None:
        """Take an in-flight tile back out and put ``tile`` at the front of pending."""
        requeued = self._run(
            self._requeue,
            keys=[self._in_flight_key, self._in_flight_data_key, self._pending_key],
            args=[json.dumps(tile, sort_keys=True), tile_id, self._ttl_seconds],
            what="requeue place tile",
        )
        if not requeued:
            raise BufferError(f"place tile {tile_id} is not in flight")

    def in_flight(self) -> list[PlaceTile]:
        raws = self._run(
            self._in_flight,
            keys=[self._in_flight_key, self._in_flight_data_key],
            args=[],
            what="read in-flight place tiles",
        )
        return [json.loads(raw) for raw in raws]

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

    def _run(
        self,
        script: Script,
        *,
        keys: list[str],
        args: list,
        what: str,
    ):
        try:
            return script(keys=keys, args=args)
        except RedisError as error:
            raise BufferError(f"could not {what}: {describe(error)}") from error
