import queue
import time
from collections.abc import Callable

import pytest

from app.services.google_map_scraper.scrape import scrape_worker
from app.services.google_map_scraper.scrape.scrape_worker import ScrapeWorker


def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


class FakeDatabase:
    def __init__(self):
        self.claimed: list[str] = []
        self.claim_next_calls = 0
        self.refuse: set[str] = set()
        self.refused_seen: set[str] = set()

    def claim(self, scrape_id):
        self.claimed.append(scrape_id)
        if scrape_id in self.refuse:
            self.refused_seen.add(scrape_id)
            return None
        return {"ID": scrape_id}

    def claim_next_pending(self):
        self.claim_next_calls += 1


@pytest.fixture
def fake_db():
    return FakeDatabase()


@pytest.fixture
def fake_run_factory(monkeypatch):
    ran: list[str] = []

    class FakeRun:
        def __init__(self, database, scrape, headless, redis, known_keys, browser=None):
            self.scrape_id = scrape["ID"]
            self.outcome = None
            self.rows = []
            ran.append(scrape["ID"])

        def execute(self):
            pass

        def pause(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(scrape_worker, "ScrapeRun", FakeRun)
    return ran


def test_assigned_worker_runs_only_the_ids_it_is_given(fake_db, fake_run_factory):
    ids = queue.Queue()
    ids.put("tile-1")
    ids.put("tile-2")
    worker = ScrapeWorker(fake_db, redis=None, assignments=ids, browser=None)
    worker.start()
    wait_until(lambda: len(fake_run_factory) == 2)
    worker.shutdown(wait=2)
    assert fake_db.claimed == ["tile-1", "tile-2"]
    assert fake_db.claim_next_calls == 0
    assert fake_run_factory == ["tile-1", "tile-2"]


def test_assigned_worker_skips_an_id_that_is_no_longer_pending(
    fake_db, fake_run_factory
):
    fake_db.refuse = {"tile-1"}
    ids = queue.Queue()
    ids.put("tile-1")
    worker = ScrapeWorker(fake_db, redis=None, assignments=ids)
    worker.start()
    wait_until(lambda: "tile-1" in fake_db.refused_seen)
    worker.shutdown(wait=2)
    assert fake_run_factory == []
