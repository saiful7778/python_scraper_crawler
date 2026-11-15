"""Fakes for the city-run coordinator: a stand-in ScrapeSession that replies per tile attempt.

``behave(keyword, slot)`` returns the :class:`Reply` for one attempt of a tile, so a
test decides what each proxy slot does with each keyword. Nothing here starts Chrome
or touches PostgreSQL.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from app.services.google_map_scraper.scrape.scrape_run import RunOutcome
from app.services.google_map_scraper.scrape.scrape_status import ScrapeStatus
from app.services.google_map_scraper.scrape.scrape_types import LeadRow, RunResult
from app.services.google_map_scraper.scraper import (
    ScrapeDeletedError,
    UnsavedLeadsError,
)


@dataclass
class Reply:
    status: str = ScrapeStatus.OK
    note: str = ""
    rows: list[LeadRow] = field(default_factory=list)
    blocked: bool = False
    connection_lost: bool = False
    deleted: bool = False
    unsaved: bool = False


def row(title: str, **overrides) -> LeadRow:
    """A lead inside the test city's bounding box, with a unique title and place id."""
    return {
        "title": title,
        "place_id": f"p-{title}",
        "latitude": 0.5,
        "longitude": 0.5,
        "phone": "",
        "website": "",
        **overrides,
    }


def done(*titles: str) -> Reply:
    return Reply(rows=[row(title) for title in titles])


def blocked(*titles: str) -> Reply:
    return Reply(rows=[row(title) for title in titles], blocked=True)


def proxy_failure() -> Reply:
    return Reply(
        status=ScrapeStatus.FAILED, note="CDPError: ERR_PROXY_CONNECTION_FAILED"
    )


def lost_chrome() -> Reply:
    return Reply(
        status=ScrapeStatus.FAILED,
        note="CDPError: navigate: connection lost: gone",
        connection_lost=True,
    )


def failed(note: str) -> Reply:
    return Reply(status=ScrapeStatus.FAILED, note=note)


class TileEnv:
    """Shared state of a test's sessions: what was created, submitted and searched.

    One TileEnv lives across the runs of a test, so a resumed run sees the scrapes
    the earlier run created.
    """

    def __init__(self, behave: Callable[[str, int], Reply], carried=()):
        self.behave = behave
        self.carried: list[str] = list(carried)
        self.scrapes: dict[str, dict] = {}
        self.replies: dict[str, Reply] = {}
        self.submitted: list[tuple[str, int]] = []
        self.resubmitted: list[str] = []
        self.searched: list[tuple[str, int]] = []
        self.finished: list[str] = []
        self.interrupt_after: int | None = None
        self.deliver_last_first = (
            False  # finish the tile submitted last, as a faster proxy would
        )
        self.delivered = 0
        self.slot_counts: list[int] = []
        self.stopped = 0
        self.deleted: set[str] = (
            set()
        )  # deleted before their worker claims them: never reported
        self._count = 0

    def slots_used_for(self, keyword: str) -> list[int]:
        return [
            slot
            for scrape_id, slot in self.submitted
            if self._keyword(scrape_id) == keyword
        ]

    def settings_of(self, scrape_id: str) -> dict:
        return self.scrapes[scrape_id]["settings"]

    def _keyword(self, scrape_id: str) -> str:
        return self.scrapes[scrape_id]["settings"]["keywords"][0]

    def factory(self):
        return lambda *args, **kwargs: TileSession(self, *args, **kwargs)


class TileSession:
    """Stands in for ``ScrapeSession``; same methods the coordinator calls."""

    def __init__(
        self, env: TileEnv, config, redis, postgres, known_keys, slots, *, locale
    ):
        self._env = env
        self._queue: deque[str] = deque()
        env.slot_counts.append(slots)

    def start(self) -> list[str]:
        return list(self._env.carried)

    def create(self, name: str, settings: dict) -> str:
        env = self._env
        env._count += 1
        scrape_id = f"scrape-{env._count}"
        env.scrapes[scrape_id] = {"name": name, "settings": dict(settings)}
        return scrape_id

    def proxies_of(self, scrape_id: str):
        record = self._env.scrapes.get(scrape_id)
        reply = self._env.replies.get(scrape_id)
        if (
            record is None
            or scrape_id in self._env.deleted
            or (reply and reply.deleted)
        ):
            return None  # a deleted scrape has no row, so it has no proxies
        return tuple(record["settings"]["proxies"])

    def submit(self, scrape_id: str, slot: int) -> None:
        env = self._env
        env.submitted.append((scrape_id, slot))
        if scrape_id in env.carried:
            env.resubmitted.append(scrape_id)
        self._queue.append((scrape_id, slot))

    def next_finished(self, timeout: float):
        env = self._env
        live = [item for item in self._queue if item[0] not in env.deleted]
        if not live:
            return None
        if env.interrupt_after is not None and env.delivered >= env.interrupt_after:
            env.interrupt_after = None
            raise KeyboardInterrupt()
        pick = live[-1] if env.deliver_last_first else live[0]
        self._queue.remove(pick)
        scrape_id, slot = pick
        keyword = env._keyword(scrape_id)
        env.searched.append((keyword, slot))
        reply = env.behave(keyword, slot)
        env.replies[scrape_id] = reply
        env.delivered += 1
        outcome = RunOutcome(
            reply.status,
            reply.note,
            blocked=reply.blocked,
            connection_lost=reply.connection_lost,
        )
        return scrape_id, outcome, reply.rows, slot

    def finish(self, scrape_id: str) -> RunResult | None:
        env = self._env
        reply = env.replies.get(scrape_id)
        if reply is None:
            return None
        if reply.deleted:
            raise ScrapeDeletedError(f"scrape {scrape_id} was deleted")
        if reply.unsaved:
            raise UnsavedLeadsError(scrape_id, 1)
        env.finished.append(scrape_id)
        return RunResult(scrape_id, reply.status, reply.note, reply.rows, reply.blocked)

    def check_workers(self) -> None:
        return None

    def stop(self) -> None:
        self._env.stopped += 1
