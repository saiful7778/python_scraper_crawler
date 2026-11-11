import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from redis import Redis
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
from app.services.buffer.keys import crawl_finished_key, crawl_key, crawl_owners_key
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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return str(uuid4())


def _restore(row: dict[str, Any]) -> dict[str, Any]:
    return {
        name: datetime.fromisoformat(value)
        if name in _DATETIME_FIELDS and value is not None
        else value
        for name, value in row.items()
    }


def _website_id_of(origin: str) -> str:
    return str(uuid5(NAMESPACE_URL, origin))


def _find(rows: list[dict[str, Any]], row_id: str, what: str) -> dict[str, Any]:
    for row in rows:
        if row["id"] == row_id:
            return row
    raise LookupError(f"crawl {what} {row_id} is not in the buffer")


class RedisCrawlBuffer:
    """Records one crawl per website as a JSON document in Redis until it is copied.

    A document is only written by the crawl of its own website, so reading and
    rewriting it needs no lock. Run and page ids are looked up in one hash that
    points to the website that holds them.
    """

    def __init__(self, redis: Redis, ttl_seconds: int):
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    def create_website(
        self,
        *,
        host: str,
        registrable_domain: str,
        origin: str,
    ) -> str:
        """Add a running website for an origin and return its id; a repeat resets it."""
        website_id = _website_id_of(origin)
        document: Document = {
            "website": {
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
            },
            "runs": [],
            "pages": [],
            "links": [],
            "findings": [],
            "events": [],
        }
        self._save(
            website_id,
            document,
            owners=[website_id],
            unfinish=True,
        )
        return website_id

    def has_website(self, origin: str) -> bool:
        """Whether a website of this origin is in the buffer, finished or not."""
        return bool(self._redis.exists(crawl_key(_website_id_of(origin))))

    def create_run(self, *, website_id: str, requested_url: str, config: dict) -> str:
        """Add a running crawl of a website and return its id."""
        document = self._read(website_id)
        run_id = _new_id()
        now = _now()
        document["runs"].append(
            {
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
        )
        self._save(website_id, document, owners=[run_id])
        return run_id

    def add_pages(self, run_id: str, pages: list[PageSeed]) -> dict[str, str]:
        """Queue pages; return normalized URL to page id, reusing existing pages."""
        website_id, document = self._open_for(run_id)
        existing = {
            page["normalized_url"]: page
            for page in document["pages"]
            if page["run_id"] == run_id
        }
        created: list[str] = []
        for seed in pages:
            if seed.normalized_url not in existing:
                page = self._new_page(
                    run_id,
                    url=seed.url,
                    normalized_url=seed.normalized_url,
                    depth=seed.depth,
                    status=_QUEUED,
                    discovery_source=seed.discovery_source,
                )
                document["pages"].append(page)
                existing[seed.normalized_url] = page
                created.append(page["id"])
        self._save(website_id, document, owners=created)
        return {
            seed.normalized_url: existing[seed.normalized_url]["id"] for seed in pages
        }

    def save_page(self, page_id: str, result: PageResult) -> None:
        """Store the outcome of fetching a page."""
        website_id, document = self._open_for(page_id)
        page = _find(document["pages"], page_id, "page")
        page.update(
            status=result.status,
            skip_reason=result.skip_reason,
            http_status=result.http_status,
            content_type=result.content_type,
            content_length=result.content_length,
            final_url=result.final_url,
            title=result.title,
            canonical_url=result.canonical_url,
            content_hash=result.content_hash,
            error_code=result.error_code,
            error_message=result.error_message,
            fetched_at=_now() if result.status == "fetched" else None,
        )
        self._save(website_id, document)

    def add_links(self, source_page_id: str, links: list[LinkSeed]) -> None:
        """Record links found on a page; unknown targets become skipped pages."""
        website_id, document = self._open_for(source_page_id)
        source = _find(document["pages"], source_page_id, "page")
        targets = {
            page["normalized_url"]: page
            for page in document["pages"]
            if page["run_id"] == source["run_id"]
        }
        linked = {
            link["target_id"]
            for link in document["links"]
            if link["source_id"] == source_page_id
        }
        created: list[str] = []
        for link in links:
            target = targets.get(link.target_normalized_url)
            if target is None:
                target = self._new_page(
                    source["run_id"],
                    url=link.target_normalized_url,
                    normalized_url=link.target_normalized_url,
                    depth=source["depth"],
                    status=_SKIPPED,
                    discovery_source="link",
                    skip_reason=_UNCRAWLED_LINK_REASON,
                )
                document["pages"].append(target)
                targets[link.target_normalized_url] = target
                created.append(target["id"])
            if target["id"] in linked:
                continue
            linked.add(target["id"])
            document["links"].append(
                {
                    "id": _new_id(),
                    "source_id": source_page_id,
                    "target_id": target["id"],
                    "anchor_text": link.anchor_text,
                    "rel": link.rel,
                    "is_internal": link.is_internal,
                    "created_at": _now(),
                }
            )
        self._save(website_id, document, owners=created)

    def add_findings(
        self,
        *,
        website_id: str,
        run_id: str,
        page_id: str | None,
        findings: list[Finding],
    ) -> None:
        """Store extracted values for a page, or for the whole site when ``page_id`` is None."""
        document = self._read(website_id)
        document["findings"].extend(
            {
                "id": _new_id(),
                "website_id": website_id,
                "run_id": run_id,
                "page_id": page_id,
                "group": finding.group,
                "key": finding.key,
                "value": finding.value,
                "extra": finding.extra,
                "created_at": _now(),
            }
            for finding in findings
        )
        self._save(website_id, document)

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
        website_id, document = self._open_for(run_id)
        document["events"].append(
            {
                "id": len(document["events"]) + 1,
                "run_id": run_id,
                "page_id": page_id,
                "step": step,
                "level": level,
                "message": message,
                "data": data,
                "created_at": _now(),
            }
        )
        self._save(website_id, document)

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
        website_id, document = self._open_for(run_id)
        finished = _now()
        run = _find(document["runs"], run_id, "run")
        run.update(
            status=status,
            progress=progress,
            error_code=error_code,
            error_message=error_message,
            finished_at=finished,
        )
        document["website"].update(
            status=status,
            error_code=error_code,
            error_message=error_message,
            finished_at=finished,
        )
        self._save(
            website_id,
            document,
            finished=status in FINISHED_STATUSES,
        )

    def fail_website(
        self, website_id: str, error_code: str, error_message: str
    ) -> None:
        """Mark a website failed when no run of it could record the outcome."""
        document = self._read(website_id)
        document["website"].update(
            status="failed",
            error_code=error_code,
            error_message=error_message,
            finished_at=_now(),
        )
        self._save(website_id, document, finished=True)

    def skip_website(self, website_id: str) -> None:
        """Mark a website that was never checked as skipped."""
        document = self._read(website_id)
        document["website"].update(status=_SKIPPED, finished_at=_now())
        self._save(website_id, document, finished=True)

    def mark_interrupted(self) -> None:
        """Fail every website and run left running by an earlier process."""
        websites = runs = 0
        for key in self._redis.scan_iter(match=crawl_key("*")):
            raw = self._redis.get(key)
            if raw is None:
                continue
            document: Document = json.loads(raw)
            website = document["website"]
            finished = _now()
            for run in document["runs"]:
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
            self._save(website["id"], document, finished=interrupted)
        if websites or runs:
            logger.warning(
                "marked %s website(s) and %s run(s) left unfinished by an earlier process as interrupted",
                websites,
                runs,
            )

    def finished_website_ids(self) -> list[str]:
        """Websites whose crawl has ended and that wait for the Postgres copy."""
        return sorted(self._redis.smembers(crawl_finished_key()))

    def finished_count(self) -> int:
        """How many websites wait for the Postgres copy."""
        return self._redis.scard(crawl_finished_key())

    def read_website(self, website_id: str) -> WebsiteSnapshot:
        """Read every buffered row of a website.

        Raises:
            LookupError: the website is not in the buffer.
        """
        document = self._read(website_id)
        return WebsiteSnapshot(
            website=_restore(document["website"]),
            runs=[_restore(row) for row in document["runs"]],
            pages=[_restore(row) for row in document["pages"]],
            links=[_restore(row) for row in document["links"]],
            findings=[_restore(row) for row in document["findings"]],
            events=[_restore(row) for row in document["events"]],
        )

    def release_website(self, website_id: str) -> None:
        """Drop the crawl data of a copied website."""
        raw = self._redis.get(crawl_key(website_id))
        ids = [website_id]
        if raw is not None:
            document: Document = json.loads(raw)
            ids += [row["id"] for row in document["runs"] + document["pages"]]
        pipe = self._redis.pipeline()
        pipe.delete(crawl_key(website_id))
        pipe.srem(crawl_finished_key(), website_id)
        pipe.hdel(crawl_owners_key(), *ids)
        pipe.execute()

    @staticmethod
    def _new_page(run_id: str, **fields: Any) -> dict[str, Any]:
        return {
            "id": _new_id(),
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

    def _open_for(self, item_id: str) -> tuple[str, Document]:
        website_id = self._redis.hget(crawl_owners_key(), item_id)
        if website_id is None:
            raise LookupError(f"crawl item {item_id} is not in the buffer")
        return website_id, self._read(website_id)

    def _read(self, website_id: str) -> Document:
        raw = self._redis.get(crawl_key(website_id))
        if raw is None:
            raise LookupError(f"crawl website {website_id} is not in the buffer")
        return json.loads(raw)

    def _save(
        self,
        website_id: str,
        document: Document,
        *,
        owners: Iterable[str] = (),
        finished: bool = False,
        unfinish: bool = False,
    ) -> None:
        key = crawl_key(website_id)
        finished_key = crawl_finished_key()
        pipe = self._redis.pipeline()
        pipe.set(key, json.dumps(document))
        pipe.expire(key, self._ttl_seconds)
        for owner in owners:
            pipe.hset(crawl_owners_key(), owner, website_id)
        pipe.expire(crawl_owners_key(), self._ttl_seconds)
        if finished:
            pipe.sadd(finished_key, website_id)
        if unfinish:
            pipe.srem(finished_key, website_id)
        pipe.expire(finished_key, self._ttl_seconds)
        try:
            pipe.execute()
        except OutOfMemoryError as exc:
            raise BufferFullError(FULL_MESSAGE) from exc
