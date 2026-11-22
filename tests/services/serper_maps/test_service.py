import asyncio

import pytest

from app.models import LeadModel
from app.repositories.serper_map_repository import SerperMapRepository
from app.services.serper.maps import serper_map_service as service_module
from app.services.serper.maps.serper_map_config import SerperMapConfig
from app.services.serper.maps.serper_map_errors import (
    SerperConfigError,
    SerperNoCreditsError,
)
from app.services.serper.maps.serper_map_plan import SerperMapPlan
from app.services.serper.maps.serper_map_service import SerperMapService
from app.services.serper.maps.serper_map_type import Place, SearchResult

from .test_rows import PLACE


def _config(**overrides):
    values = {"keywords": ("plumbers",), "city": "Austin", "state": "TX", "country": "US"}
    values.update(overrides)
    return SerperMapConfig(**values)


async def _fake_plan(config=None):
    config = config or _config()
    from app.repositories.serper_map_repository import SerperMapPage

    pages = [
        SerperMapPage("plumbers", page, 0, 0, 30.26, -97.74, 15) for page in (1, 2)
    ]
    return SerperMapPlan(config=config, center=(30.26, -97.74), bounding_box=None, pages=pages)


def _result_with_two_places():
    second = Place(
        position=2,
        title="Second Cafe",
        latitude=30.27,
        longitude=-97.75,
        placeId="p2",
        fid="0x1:0x2",
        phoneNumber="(512) 555-0101",
        website="https://second.example",
    )
    return SearchResult(ll="", places=[PLACE, second], credits=3)


class FakeClient:
    def __init__(self, index, answers):
        if isinstance(answers, SearchResult):
            answers = [answers]
        self.index = index
        self.answers = list(answers or [])
        #: A single answer repeats, so one script can serve every page.
        self.default = self.answers[-1] if len(self.answers) == 1 else SearchResult(ll="", places=[], credits=3)
        self.calls = []

    async def search(self, keyword, lat, lon, zoom, page):
        self.calls.append((keyword, page))
        answer = self.answers.pop(0) if self.answers else self.default
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def balance(self) -> int:
        return 100

    async def aclose(self):
        pass


def _service(keys=("k1",), answers=None, clients=None, **overrides):
    if isinstance(answers, SearchResult):
        answers = [answers]
    factory_clients = clients or {
        key: FakeClient(index, answers or []) for index, key in enumerate(keys)
    }
    service = SerperMapService(
        _config(**overrides),
        api_keys=list(keys),
        client_factory=lambda key: factory_clients[key],
    )
    return service


async def test_start_writes_the_job_and_every_request(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    service = _service()
    job_id = await service.start()
    info = await SerperMapRepository().get(job_id)
    assert info is not None
    assert info.total_requests == 2
    assert info.pending_requests == 2
    assert info.status == "running"
    await service.aclose()


async def test_run_saves_the_places_of_every_page(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    service = _service(answers=_result_with_two_places())
    job_id = await service.start()
    result = await service.run(job_id)
    assert result.status == "finished"
    assert result.requests_done == 2
    assert result.places_found == 4
    assert result.places_saved == 2
    assert await LeadModel.all().count() == 2
    assert result.credits_spent == 6
    await service.aclose()


async def test_a_second_job_does_not_duplicate_the_rows(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    first = _service(answers=_result_with_two_places())
    await first.run(await first.start())
    second = _service(answers=_result_with_two_places())
    await second.run(await second.start())
    assert await LeadModel.all().count() == 2
    await first.aclose()
    await second.aclose()


async def test_known_keys_are_skipped(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    from app.repositories import GoogleMapProfileRepository, LeadRepository
    from app.services.google_map_scraper.scrape.dedupe import seen_keys_of

    first = _service(answers=_result_with_two_places())
    await first.run(await first.start())
    known = frozenset(
        seen_keys_of(await GoogleMapProfileRepository(LeadRepository()).key_rows())
    )
    second = SerperMapService(
        _config(),
        api_keys=["k1"],
        known_keys=known,
        client_factory=lambda key: FakeClient(0, _result_with_two_places()),
    )
    service_result = await second.run(await second.start())
    assert service_result.places_saved == 0
    assert await LeadModel.all().count() == 2
    await first.aclose()
    await second.aclose()


async def test_an_exhausted_run_is_not_failed_and_stays_pending(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    service = _service(answers=[SerperNoCreditsError("out")] * 4)
    job_id = await service.start()
    result = await service.run(job_id)
    assert result.status == "exhausted"
    assert result.requests_pending == 2
    assert result.requests_failed == 0
    stored = await SerperMapRepository().get(job_id)
    assert stored is not None and stored.status == "exhausted"
    await service.aclose()


async def test_resume_skips_done_pages_and_retries_the_rest(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    first = _service(answers=[SerperNoCreditsError("out")] * 4)
    job_id = await first.start()
    await first.run(job_id)

    resumed = _service(answers=_result_with_two_places())
    result = await resumed.run(job_id)
    assert result.requests_done == 2
    assert result.status == "finished"
    await first.aclose()
    await resumed.aclose()


async def test_find_unfinished_matches_the_same_config_only(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    service = _service(answers=[SerperNoCreditsError("out")] * 4)
    job_id = await service.start()
    await service.run(job_id)
    unfinished = await service.find_unfinished()
    assert unfinished is not None and unfinished.id == job_id
    assert await _service(depth=2).find_unfinished() is None
    await service.aclose()


async def test_cancel_marks_the_job_interrupted_and_keeps_progress(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    service = _service(answers=_result_with_two_places())
    job_id = await service.start()
    task = asyncio.create_task(service.run(job_id))
    await asyncio.sleep(0)
    await service.cancel()
    result = await task
    assert result.status == "interrupted"
    stored = await SerperMapRepository().get(job_id)
    assert stored is not None and stored.status == "interrupted"
    await service.aclose()


async def test_cancelling_the_task_keeps_the_progress_that_was_saved(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)

    class Blocking(FakeClient):
        async def search(self, keyword, lat, lon, zoom, page):
            await asyncio.sleep(1)
            return SearchResult(ll="", places=[], credits=3)

    clients = {"k1": Blocking(0, [])}
    service = SerperMapService(
        _config(), api_keys=["k1"], client_factory=lambda key: clients[key]
    )
    job_id = await service.start()
    task = asyncio.create_task(service.run(job_id))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await SerperMapRepository().get(job_id)
    assert stored is not None and stored.status == "interrupted"
    await service.aclose()


async def test_the_crawlability_check_runs_only_with_has_website(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    checked = []

    def crawl(rows):
        checked.append(len(rows))
        for row in rows:
            row["is_crawlable"] = True

    with_site = _service(answers=_result_with_two_places(), has_website=True)
    with_site._crawlability = crawl
    await with_site.run(await with_site.start())
    # The second page repeats the same two places, so dedupe leaves nothing for it.
    assert checked == [2]
    assert await LeadModel.filter(is_crawlable=True).count() == 2

    checked.clear()
    without = _service(answers=_result_with_two_places())
    without._crawlability = crawl
    await without.run(await without.start())
    assert checked == []
    await with_site.aclose()
    await without.aclose()


async def test_no_api_key_is_a_typed_error(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    with pytest.raises(SerperConfigError):
        _service(keys=())


async def test_a_key_with_too_little_balance_is_not_used(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)

    class Poor(FakeClient):
        async def balance(self) -> int:
            return 2

    clients = {
        "poor": Poor(0, []),
        "rich": FakeClient(1, _result_with_two_places()),
    }
    service = SerperMapService(
        _config(), api_keys=["poor", "rich"], client_factory=lambda key: clients[key]
    )
    result = await service.run(await service.start())
    assert result.status == "finished"
    assert clients["poor"].calls == []
    await service.aclose()


async def test_rows_outside_the_city_are_dropped_on_a_grid(db, monkeypatch):
    from app.repositories.serper_map_repository import SerperMapPage

    async def far_plan(config=None):
        pages = [SerperMapPage("plumbers", 1, 0, 0, 30.26, -97.74, 13)]
        return SerperMapPlan(
            config=_config(grid=2),
            center=(30.26, -97.74),
            bounding_box=(30.09, 30.51, -97.93, -97.56),
            pages=pages,
        )

    monkeypatch.setattr(service_module, "build_plan", far_plan)
    outside = Place(
        position=1, title="Far Away", latitude=40.0, longitude=-80.0, placeId="far"
    )
    result_places = SearchResult(ll="", places=[PLACE, outside], credits=3)
    service = _service(answers=[result_places])
    result = await service.run(await service.start())
    assert result.places_saved == 1
    assert await LeadModel.filter(title="Far Away").count() == 0
    await service.aclose()


async def test_a_job_with_no_pending_page_finishes_without_a_request(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    service = _service(answers=_result_with_two_places())
    job_id = await service.start()
    await service.run(job_id)

    again = await service.run(job_id)
    assert again.status == "finished"
    assert again.requests_pending == 0
    await service.aclose()


async def test_a_run_whose_keys_are_all_refused_fails(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)

    class Poor(FakeClient):
        async def balance(self) -> int:
            return 0

    service = SerperMapService(
        _config(), api_keys=["k1"], client_factory=lambda key: Poor(0, [])
    )
    job_id = await service.start()
    result = await service.run(job_id)
    assert result.status == "failed"
    assert "credits" in result.error
    stored = await SerperMapRepository().get(job_id)
    assert stored is not None and stored.status == "failed"
    await service.aclose()
