import asyncio

import pytest
from tortoise.context import TortoiseContext

from app.models import GoogleMapProfileModel, LeadModel
from app.repositories.google_map_profile_repository import GoogleMapProfileRepository
from app.repositories.lead_repository import LeadRepository
from app.services.buffer.flusher import FLUSH_SECONDS, Flusher
from app.services.buffer.keys import state_key
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from config.tortoise import TortoiseConfig


class Saved:
    def __init__(self, fail_on=()):
        self.calls: list[tuple[str, str]] = []
        self._fail_on = set(fail_on)

    async def __call__(self, row, scrape_id):
        if row["title"] in self._fail_on:
            raise RuntimeError("database is down")
        self.calls.append((scrape_id, row["title"]))


@pytest.fixture
def rows(fake_redis):
    return RowBuffer(fake_redis, 3600)


@pytest.fixture
def state(fake_redis):
    return ScrapeState(fake_redis, 3600)


@pytest.fixture
def make_flusher(state):
    flushers = []

    def make(rows, save_row):
        flusher = Flusher(rows, state, save_row=save_row, crawl_sink=None)
        flushers.append(flusher)
        return flusher

    yield make
    for flusher in flushers:
        flusher.shutdown()


def test_flush_interval_is_five_seconds():
    assert FLUSH_SECONDS == 5


def test_flush_saves_rows_in_order_and_removes_them(rows, state, make_flusher):
    rows.append("s1", [{"title": "A"}, {"title": "B"}])
    save = Saved()
    assert make_flusher(rows, save).flush_once() == 2
    assert save.calls == [("s1", "A"), ("s1", "B")]
    assert rows.length("s1") == 0


def test_flush_with_nothing_buffered_saves_nothing(rows, state, make_flusher):
    assert make_flusher(rows, Saved()).flush_once() == 0


def test_failed_flush_keeps_rows_for_next_tick(rows, state, make_flusher):
    rows.append("s1", [{"title": "A"}])
    flusher = make_flusher(rows, Saved(fail_on={"A"}))
    assert flusher.flush_once() == 0
    assert rows.length("s1") == 1


def test_flush_stops_at_the_first_failed_row_and_keeps_the_rest(
    rows, state, make_flusher
):
    rows.append("s1", [{"title": "A"}, {"title": "B"}, {"title": "C"}])
    save = Saved(fail_on={"B"})
    assert make_flusher(rows, save).flush_once() == 1
    assert save.calls == [("s1", "A")]
    assert [row["title"] for row in rows.read_all("s1")] == ["B", "C"]


def test_flush_retries_a_failed_row_on_the_next_tick(rows, state, make_flusher):
    rows.append("s1", [{"title": "A"}, {"title": "B"}])
    save = Saved(fail_on={"A"})
    flusher = make_flusher(rows, save)
    flusher.flush_once()
    save._fail_on.clear()
    assert flusher.flush_once() == 2
    assert rows.length("s1") == 0


def test_a_failing_scrape_does_not_stop_other_scrapes(rows, state, make_flusher):
    rows.append("s1", [{"title": "A"}])
    rows.append("s2", [{"title": "B"}])
    save = Saved(fail_on={"A"})
    assert make_flusher(rows, save).flush_once() == 1
    assert save.calls == [("s2", "B")]
    assert rows.length("s1") == 1
    assert rows.length("s2") == 0


def test_flush_saves_at_most_500_rows_per_scrape_per_tick(rows, state, make_flusher):
    rows.append("s1", [{"title": str(i)} for i in range(501)])
    flusher = make_flusher(rows, Saved())
    assert flusher.flush_once() == 500
    assert rows.length("s1") == 1


def test_flush_refreshes_the_ttl_of_every_scrape_it_touches(
    fake_redis, rows, state, make_flusher
):
    state.create("s1", {})
    rows.append("s1", [{"title": "A"}])
    fake_redis.persist(state_key("s1"))
    make_flusher(rows, Saved(fail_on={"A"})).flush_once()
    assert 0 < fake_redis.ttl(state_key("s1")) <= 3600


def test_run_flushes_until_shutdown(rows, state):
    rows.append("s1", [{"title": "A"}])
    flusher = Flusher(rows, state, save_row=Saved(), crawl_sink=None, interval=0.01)
    flusher.start()
    flusher.shutdown(wait=2)
    assert not flusher.is_alive()
    assert rows.length("s1") == 0


class TrimFailsOnce(RowBuffer):
    def __init__(self, inner):
        self.__dict__.update(inner.__dict__)
        self.failed = False

    def trim(self, scrape_id, count):
        if not self.failed:
            self.failed = True
            raise ConnectionError("redis went away")
        super().trim(scrape_id, count)


def sqlite_config(path):
    return TortoiseConfig(
        connections={
            "default": {
                "engine": "tortoise.backends.sqlite",
                "credentials": {"file_path": str(path)},
            }
        }
    )


def in_database(config, work=None, generate_schemas=False):
    async def run():
        with TortoiseContext() as context:
            await context.init(
                config={
                    "connections": {"flusher_check": config.connections["default"]},
                    "apps": {
                        "models": {
                            "models": ["app.models"],
                            "default_connection": "flusher_check",
                        }
                    },
                }
            )
            try:
                if generate_schemas:
                    await context.generate_schemas(safe=True)
                return await work() if work else None
            finally:
                await context.close_connections()

    return asyncio.run(run())


def test_unreachable_database_keeps_rows_and_does_not_raise(tmp_path, rows, state):
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("")
    rows.append("s1", [{"place_id": "p1", "title": "A"}])
    flusher = Flusher(
        rows,
        state,
        save_row=GoogleMapProfileRepository(LeadRepository()).save_row,
        crawl_sink=None,
        config=sqlite_config(not_a_directory / "leads.db"),
    )
    try:
        assert flusher.flush_once() == 0
        assert rows.length("s1") == 1
    finally:
        flusher.shutdown()


def test_replayed_flush_does_not_duplicate_leads(tmp_path, rows, state):
    config = sqlite_config(tmp_path / "leads.db")
    in_database(config, generate_schemas=True)
    rows.append(
        "s1",
        [
            {"place_id": "p1", "title": "Cafe A", "website": "https://a.com"},
            {"place_id": "p2", "title": "Cafe B", "website": "https://b.com"},
        ],
    )
    repo = GoogleMapProfileRepository(LeadRepository())
    flusher = Flusher(
        TrimFailsOnce(rows),
        state,
        save_row=repo.save_row,
        crawl_sink=None,
        config=config,
    )
    try:
        with pytest.raises(ConnectionError):
            flusher.flush_once()
        assert rows.length("s1") == 2
        assert flusher.flush_once() == 2
    finally:
        flusher.shutdown()

    async def counts():
        return (
            await GoogleMapProfileModel.filter(place_id="p1").count(),
            await GoogleMapProfileModel.filter(place_id="p2").count(),
            await LeadModel.all().count(),
        )

    assert rows.length("s1") == 0
    assert in_database(config, counts) == (1, 1, 2)
