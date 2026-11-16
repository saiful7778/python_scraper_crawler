import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from redis import Redis
from redis.client import Pipeline
from redis.commands.core import Script
from redis.exceptions import OutOfMemoryError

from app.services.buffer.client import FULL_MESSAGE, BufferFullError
from app.services.buffer.crawl_types import (
    FINISHED_STATUSES,
    WEBSITE_TECH_FIELDS,
    LinkSeed,
    PageResult,
    PageSeed,
    WebsiteSnapshot,
)
from app.services.buffer.keys import (
    crawl_events_key,
    crawl_findings_key,
    crawl_finished_key,
    crawl_key,
    crawl_links_key,
    crawl_owners_key,
    crawl_pages_key,
    crawl_runs_key,
)
from app.services.crawler.findings import Finding
from app.utils.logger import get_logger

logger = get_logger(__name__)

_RUNNING = "running"
_QUEUED = "queued"
_SKIPPED = "skipped"
_UNCRAWLED_LINK_REASON = "out_of_scope"
_INTERRUPTED = "interrupted"
_DATETIME_FIELDS = frozenset({"created_at", "started_at", "finished_at", "fetched_at"})

Document = dict[str, Any]

_ADD_FINDINGS = """
if redis.call('EXISTS', KEYS[1]) == 0 then
    return 0
end
for i = 2, #ARGV do
    redis.call('RPUSH', KEYS[2], ARGV[i])
end
for i = 1, #KEYS do
    redis.call('EXPIRE', KEYS[i], ARGV[1])
end
return 1
"""

_ADD_PAGES = """
for i = 3, #ARGV, 2 do
    if redis.call('HSETNX', KEYS[1], ARGV[i], ARGV[i + 1]) == 1 then
        redis.call('HSET', KEYS[2], ARGV[i], ARGV[2])
    end
end
for i = 1, #KEYS do
    redis.call('EXPIRE', KEYS[i], ARGV[1])
end
return 1
"""

_ADD_LINKS = """
for i = 3, #ARGV, 4 do
    redis.call('HSETNX', KEYS[1], ARGV[i], ARGV[i + 2])
    redis.call('HSET', KEYS[2], ARGV[i], ARGV[2])
    redis.call('HSETNX', KEYS[3], ARGV[i + 1], ARGV[i + 3])
end
for i = 1, #KEYS do
    redis.call('EXPIRE', KEYS[i], ARGV[1])
end
return 1
"""

_UPDATE_PAGE = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then
    return 0
end
local page = cjson.decode(raw)
local patch = cjson.decode(ARGV[2])
for key, value in pairs(patch) do
    page[key] = value
end
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(page))
for i = 1, #KEYS do
    redis.call('EXPIRE', KEYS[i], ARGV[3])
end
return 1
"""

_ADD_EVENT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
    return 0
end
local event = cjson.decode(ARGV[2])
event['id'] = redis.call('LLEN', KEYS[2]) + 1
redis.call('RPUSH', KEYS[2], cjson.encode(event))
for i = 1, #KEYS do
    redis.call('EXPIRE', KEYS[i], ARGV[1])
end
return event['id']
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return str(uuid4())


def _page_id(run_id: str, normalized_url: str) -> str:
    """The id of a page is fixed by its run and URL, so a repeat finds it again."""
    return str(uuid5(NAMESPACE_URL, f"{run_id}:{normalized_url}"))


def _link_id(source_page_id: str, target_page_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{source_page_id}:{target_page_id}"))


def _restore(row: dict[str, Any]) -> dict[str, Any]:
    return {
        name: datetime.fromisoformat(value)
        if name in _DATETIME_FIELDS and value is not None
        else value
        for name, value in row.items()
    }


def _website_id_of(origin: str) -> str:
    return str(uuid5(NAMESPACE_URL, origin))


def _expire_all(pipe: Pipeline, keys: list[str], ttl_seconds: int) -> None:
    for key in keys:
        pipe.expire(key, ttl_seconds)


def _sorted_rows(values: list[str]) -> list[dict[str, Any]]:
    """Rows of a hash, in the order they were created."""
    rows = [json.loads(value) for value in values]
    return sorted(rows, key=lambda row: (row.get("created_at") or "", row["id"]))


class RedisCrawlBuffer:
    """Records one crawl per website in Redis until it is copied.

    A website is one JSON string; its runs, pages and links are hashes keyed by
    id, and its findings and events are lists. A write therefore stores only the
    row it changed, instead of rewriting the whole crawl.
    """

    def __init__(self, redis: Redis, ttl_seconds: int):
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._add_findings = redis.register_script(_ADD_FINDINGS)
        self._add_pages = redis.register_script(_ADD_PAGES)
        self._add_links = redis.register_script(_ADD_LINKS)
        self._update_page = redis.register_script(_UPDATE_PAGE)
        self._add_event = redis.register_script(_ADD_EVENT)

    def create_website(
        self,
        *,
        host: str,
        registrable_domain: str,
        origin: str,
    ) -> str:
        """Add a running website for an origin and return its id; a repeat resets it."""
        website_id = _website_id_of(origin)
        website: Document = {
            "id": website_id,
            "host": host,
            "registrable_domain": registrable_domain,
            "origin": origin,
            "status": _RUNNING,
            "error_code": None,
            "error_message": None,
            "created_at": _now(),
            "finished_at": None,
            **dict.fromkeys(WEBSITE_TECH_FIELDS),
        }
        pipe = self._redis.pipeline()
        pipe.set(crawl_key(website_id), json.dumps(website))
        pipe.delete(
            crawl_runs_key(website_id),
            crawl_pages_key(website_id),
            crawl_links_key(website_id),
            crawl_findings_key(website_id),
            crawl_events_key(website_id),
        )
        pipe.hset(crawl_owners_key(), website_id, website_id)
        pipe.srem(crawl_finished_key(), website_id)
        _expire_all(
            pipe,
            [crawl_key(website_id), crawl_owners_key(), crawl_finished_key()],
            self._ttl_seconds,
        )
        self._execute(pipe)
        return website_id

    def has_website(self, origin: str) -> bool:
        """Whether a website of this origin is in the buffer, finished or not."""
        return bool(self._redis.exists(crawl_key(_website_id_of(origin))))

    def create_run(self, *, website_id: str, requested_url: str, config: dict) -> str:
        """Add a running crawl of a website and return its id."""
        if not self._redis.exists(crawl_key(website_id)):
            raise LookupError(f"crawl website {website_id} is not in the buffer")
        run_id = _new_id()
        now = _now()
        run: Document = {
            "id": run_id,
            "website_id": website_id,
            "requested_url": requested_url,
            "status": _RUNNING,
            "config": config,
            "progress": {},
            "error_code": None,
            "error_message": None,
            "started_at": now,
            "finished_at": None,
            "created_at": now,
        }
        pipe = self._redis.pipeline()
        pipe.hset(crawl_runs_key(website_id), run_id, json.dumps(run))
        pipe.hset(crawl_owners_key(), run_id, website_id)
        _expire_all(
            pipe,
            [
                crawl_key(website_id),
                crawl_runs_key(website_id),
                crawl_owners_key(),
                crawl_finished_key(),
            ],
            self._ttl_seconds,
        )
        self._execute(pipe)
        return run_id

    def add_pages(self, run_id: str, pages: list[PageSeed]) -> dict[str, str]:
        """Queue pages; return normalized URL to page id, reusing existing pages."""
        website_id = cast(str, self._redis.hget(crawl_owners_key(), run_id))
        if website_id is None:
            raise LookupError(f"crawl item {run_id} is not in the buffer")
        now = _now()
        seeds = {seed.normalized_url: seed for seed in pages}
        page_ids = {
            normalized_url: _page_id(run_id, normalized_url)
            for normalized_url in seeds
        }
        args: list[Any] = [self._ttl_seconds, website_id]
        for normalized_url, seed in seeds.items():
            page = self._new_page(
                run_id,
                page_id=page_ids[normalized_url],
                url=seed.url,
                normalized_url=normalized_url,
                depth=seed.depth,
                status=_QUEUED,
                discovery_source=seed.discovery_source,
                created_at=now,
            )
            args.extend([page["id"], json.dumps(page)])
        self._run(
            self._add_pages,
            keys=[
                crawl_pages_key(website_id),
                crawl_owners_key(),
                crawl_key(website_id),
                crawl_finished_key(),
            ],
            args=args,
        )
        return page_ids

    def save_page(self, page_id: str, result: PageResult) -> None:
        """Store the outcome of fetching a page."""
        website_id = cast(str, self._redis.hget(crawl_owners_key(), page_id))
        if website_id is None:
            raise LookupError(f"crawl item {page_id} is not in the buffer")
        patch = {
            "status": result.status,
            "skip_reason": result.skip_reason,
            "http_status": result.http_status,
            "content_type": result.content_type,
            "content_length": result.content_length,
            "final_url": result.final_url,
            "title": result.title,
            "canonical_url": result.canonical_url,
            "content_hash": result.content_hash,
            "error_code": result.error_code,
            "error_message": result.error_message,
            "fetched_at": _now() if result.status == "fetched" else None,
        }
        updated = self._run(
            self._update_page,
            keys=[
                crawl_pages_key(website_id),
                crawl_key(website_id),
                crawl_owners_key(),
                crawl_finished_key(),
            ],
            args=[page_id, json.dumps(patch), self._ttl_seconds],
        )
        if not updated:
            raise LookupError(f"crawl item {page_id} is not in the buffer")

    def add_links(self, source_page_id: str, links: list[LinkSeed]) -> None:
        """Record links found on a page; unknown targets become skipped pages."""
        website_id = cast(str, self._redis.hget(crawl_owners_key(), source_page_id))
        if website_id is None:
            raise LookupError(f"crawl item {source_page_id} is not in the buffer")
        raw = self._redis.hget(crawl_pages_key(website_id), source_page_id)
        if raw is None:
            raise LookupError(f"crawl item {source_page_id} is not in the buffer")
        source = json.loads(raw)
        now = _now()
        args: list[Any] = [self._ttl_seconds, website_id]
        for link in links:
            target_id = _page_id(source["run_id"], link.target_normalized_url)
            link_id = _link_id(source_page_id, target_id)
            args.extend(
                [
                    target_id,
                    link_id,
                    json.dumps(
                        self._new_page(
                            source["run_id"],
                            page_id=target_id,
                            url=link.target_normalized_url,
                            normalized_url=link.target_normalized_url,
                            depth=source["depth"],
                            status=_SKIPPED,
                            discovery_source="link",
                            skip_reason=_UNCRAWLED_LINK_REASON,
                            created_at=now,
                        )
                    ),
                    json.dumps(
                        {
                            "id": link_id,
                            "source_id": source_page_id,
                            "target_id": target_id,
                            "anchor_text": link.anchor_text,
                            "rel": link.rel,
                            "is_internal": link.is_internal,
                            "created_at": now,
                        }
                    ),
                ]
            )
        self._run(
            self._add_links,
            keys=[
                crawl_pages_key(website_id),
                crawl_owners_key(),
                crawl_links_key(website_id),
                crawl_key(website_id),
                crawl_finished_key(),
            ],
            args=args,
        )

    def add_findings(
        self,
        *,
        website_id: str,
        run_id: str,
        page_id: str | None,
        findings: list[Finding],
    ) -> None:
        """Store extracted values for a page, or for the whole site when ``page_id`` is None."""
        now = _now()
        rows = [
            json.dumps(
                {
                    "id": _new_id(),
                    "website_id": website_id,
                    "run_id": run_id,
                    "page_id": page_id,
                    "group": finding.group,
                    "key": finding.key,
                    "value": finding.value,
                    "extra": finding.extra,
                    "created_at": now,
                }
            )
            for finding in findings
        ]
        added = self._add_findings(
            keys=[
                crawl_key(website_id),
                crawl_findings_key(website_id),
                crawl_owners_key(),
                crawl_finished_key(),
            ],
            args=[self._ttl_seconds, *rows],
        )
        if not added:
            raise LookupError(f"crawl website {website_id} is not in the buffer")

    def add_event(
        self,
        *,
        run_id: str,
        step: str,
        level: str,
        message: str,
        page_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        """Log a step of the crawl."""
        website_id = cast(str, self._redis.hget(crawl_owners_key(), run_id))
        if website_id is None:
            raise LookupError(f"crawl item {run_id} is not in the buffer")
        event = json.dumps(
            {
                "run_id": run_id,
                "page_id": page_id,
                "step": step,
                "level": level,
                "message": message,
                "data": data,
                "created_at": _now(),
            }
        )
        added = self._add_event(
            keys=[
                crawl_key(website_id),
                crawl_events_key(website_id),
                crawl_owners_key(),
                crawl_finished_key(),
            ],
            args=[self._ttl_seconds, event],
        )
        if not added:
            raise LookupError(f"crawl website {website_id} is not in the buffer")

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        progress: dict,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        """End a run and give its website the same status."""
        pipe = self._redis.pipeline()
        pipe.hget(crawl_owners_key(), run_id)
        website_id = cast(str, pipe.execute()[0])
        if website_id is None:
            raise LookupError(f"crawl item {run_id} is not in the buffer")
        pipe = self._redis.pipeline()
        pipe.hget(crawl_runs_key(website_id), run_id)
        pipe.get(crawl_key(website_id))
        raw_run, raw_website = pipe.execute()
        if raw_run is None or raw_website is None:
            raise LookupError(f"crawl item {run_id} is not in the buffer")
        finished = _now()
        run = json.loads(raw_run)
        run.update(
            status=status,
            progress=progress,
            error_code=error_code,
            error_message=error_message,
            finished_at=finished,
        )
        website = json.loads(raw_website)
        website.update(
            status=status,
            error_code=error_code,
            error_message=error_message,
            finished_at=finished,
        )
        pipe = self._redis.pipeline()
        pipe.hset(crawl_runs_key(website_id), run_id, json.dumps(run))
        pipe.set(crawl_key(website_id), json.dumps(website))
        if status in FINISHED_STATUSES:
            pipe.sadd(crawl_finished_key(), website_id)
        else:
            pipe.srem(crawl_finished_key(), website_id)
        _expire_all(
            pipe,
            [
                crawl_key(website_id),
                crawl_runs_key(website_id),
                crawl_owners_key(),
                crawl_finished_key(),
            ],
            self._ttl_seconds,
        )
        self._execute(pipe)

    def fail_website(
        self, website_id: str, error_code: str, error_message: str
    ) -> None:
        """Mark a website failed when no run of it could record the outcome."""
        raw = self._redis.get(crawl_key(website_id))
        if raw is None:
            raise LookupError(f"crawl website {website_id} is not in the buffer")
        website = json.loads(raw)
        website.update(
            status="failed",
            error_code=error_code,
            error_message=error_message,
            finished_at=_now(),
        )
        self._save_website(website_id, website, finished=True)

    def skip_website(self, website_id: str) -> None:
        """Mark a website that was never checked as skipped."""
        raw = self._redis.get(crawl_key(website_id))
        if raw is None:
            raise LookupError(f"crawl website {website_id} is not in the buffer")
        website = json.loads(raw)
        website.update(status=_SKIPPED, finished_at=_now())
        self._save_website(website_id, website, finished=True)

    def mark_interrupted(self) -> None:
        """Fail every website and run left running by an earlier process."""
        websites = runs = 0
        for key in self._redis.scan_iter(match=crawl_key("*")):
            # The runs, pages, links, findings and events keys extend the website
            # key, so only the website key itself is handled here.
            if key.count(":") != 2:
                continue
            pipe = self._redis.pipeline()
            pipe.get(key)
            pipe.hgetall(crawl_runs_key(key.split(":", 2)[2]))
            raw, raw_runs = pipe.execute()
            if raw is None:
                continue
            website = json.loads(raw)
            website_id = website["id"]
            finished = _now()
            run_rows = [json.loads(row) for row in raw_runs.values()]
            for run in run_rows:
                if run["status"] in (_RUNNING, _QUEUED):
                    run.update(
                        status="failed",
                        error_code=_INTERRUPTED,
                        finished_at=finished,
                    )
                    runs += 1
            interrupted = website["status"] == _RUNNING
            if interrupted:
                website.update(
                    status="failed",
                    error_code=_INTERRUPTED,
                    finished_at=finished,
                )
                websites += 1
            pipe = self._redis.pipeline()
            for run in run_rows:
                pipe.hset(crawl_runs_key(website_id), run["id"], json.dumps(run))
            pipe.set(key, json.dumps(website))
            if interrupted:
                pipe.sadd(crawl_finished_key(), website_id)
            _expire_all(
                pipe,
                [
                    key,
                    crawl_runs_key(website_id),
                    crawl_owners_key(),
                    crawl_finished_key(),
                ],
                self._ttl_seconds,
            )
            self._execute(pipe)
        if websites or runs:
            logger.warning(
                "marked %s website(s) and %s run(s) left unfinished by an earlier process as interrupted",
                websites,
                runs,
            )

    def finished_website_ids(self) -> list[str]:
        """Websites whose crawl has ended and that wait for the Postgres copy."""
        members = self._redis.smembers(crawl_finished_key())
        return sorted(str(member) for member in members)

    def finished_count(self) -> int:
        """How many websites wait for the Postgres copy."""
        return self._redis.scard(crawl_finished_key())

    def read_website(self, website_id: str) -> WebsiteSnapshot:
        """Read every buffered row of a website.

        Raises:
            LookupError: the website is not in the buffer.
        """
        pipe = self._redis.pipeline()
        pipe.get(crawl_key(website_id))
        pipe.hgetall(crawl_runs_key(website_id))
        pipe.hgetall(crawl_pages_key(website_id))
        pipe.hgetall(crawl_links_key(website_id))
        pipe.lrange(crawl_findings_key(website_id), 0, -1)
        pipe.lrange(crawl_events_key(website_id), 0, -1)
        raw, runs, pages, links, findings, events = pipe.execute()
        if raw is None:
            raise LookupError(f"crawl website {website_id} is not in the buffer")
        return WebsiteSnapshot(
            website=_restore(json.loads(raw)),
            runs=[_restore(row) for row in _sorted_rows(list(runs.values()))],
            pages=[_restore(row) for row in _sorted_rows(list(pages.values()))],
            links=[_restore(row) for row in _sorted_rows(list(links.values()))],
            findings=[_restore(json.loads(row)) for row in findings],
            events=[_restore(json.loads(row)) for row in events],
        )

    def release_website(self, website_id: str) -> None:
        """Drop the crawl data of a copied website."""
        pipe = self._redis.pipeline()
        pipe.hkeys(crawl_runs_key(website_id))
        pipe.hkeys(crawl_pages_key(website_id))
        run_ids, page_ids = pipe.execute()
        ids = [website_id, *run_ids, *page_ids]
        pipe = self._redis.pipeline()
        pipe.delete(
            crawl_key(website_id),
            crawl_runs_key(website_id),
            crawl_pages_key(website_id),
            crawl_links_key(website_id),
            crawl_findings_key(website_id),
            crawl_events_key(website_id),
        )
        pipe.srem(crawl_finished_key(), website_id)
        pipe.hdel(crawl_owners_key(), *ids)
        self._execute(pipe)

    def _save_website(
        self, website_id: str, website: dict[str, Any], *, finished: bool
    ) -> None:
        pipe = self._redis.pipeline()
        pipe.set(crawl_key(website_id), json.dumps(website))
        if finished:
            pipe.sadd(crawl_finished_key(), website_id)
        else:
            pipe.srem(crawl_finished_key(), website_id)
        _expire_all(
            pipe,
            [
                crawl_key(website_id),
                crawl_owners_key(),
                crawl_finished_key(),
            ],
            self._ttl_seconds,
        )
        self._execute(pipe)

    def _run(self, script: Script, *, keys: list[str], args: list[Any]) -> Any:
        try:
            return script(keys=keys, args=args)
        except OutOfMemoryError as exc:
            raise BufferFullError(FULL_MESSAGE) from exc

    def _execute(self, pipe: Pipeline) -> list[Any]:
        try:
            return pipe.execute()
        except OutOfMemoryError as exc:
            raise BufferFullError(FULL_MESSAGE) from exc

    @staticmethod
    def _new_page(run_id: str, *, page_id: str, **fields: Any) -> dict[str, Any]:
        return {
            "id": page_id,
            "run_id": run_id,
            "skip_reason": None,
            "http_status": None,
            "content_type": None,
            "content_length": None,
            "final_url": None,
            "title": None,
            "canonical_url": None,
            "content_hash": None,
            "fetched_at": None,
            "error_code": None,
            "error_message": None,
            "created_at": _now(),
            **fields,
        }
