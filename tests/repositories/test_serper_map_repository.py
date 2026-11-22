from uuid import uuid4

from app.models import SerperMapJobStatus, SerperMapRequestModel
from app.repositories.serper_map_repository import SerperMapPage, SerperMapRepository

PAGES = [
    SerperMapPage("plumbers", 1, 0, 0, 30.1, -97.7, 13),
    SerperMapPage("plumbers", 2, 0, 0, 30.1, -97.7, 13),
    SerperMapPage("plumbers", 1, 1, 0, 30.2, -97.6, 13),
]


async def test_create_job_writes_one_request_row_per_page(db):
    repo = SerperMapRepository()
    info = await repo.create_job("plumbers @ Austin", {"city": "Austin"}, "k1", PAGES, 4)
    assert info.total_requests == 3
    assert info.pending_requests == 3
    assert info.status == "running"
    assert await SerperMapRequestModel.filter(job_id=info.id).count() == 3


async def test_find_unfinished_matches_the_config_key_only(db):
    repo = SerperMapRepository()
    job = await repo.create_job("j", {}, "k1", PAGES[:1], 1)
    assert (await repo.find_unfinished("k1")).id == job.id
    assert await repo.find_unfinished("other") is None


async def test_find_unfinished_skips_finished_and_failed_jobs(db):
    repo = SerperMapRepository()
    finished = await repo.create_job("j", {}, "k1", PAGES[:1], 1)
    await repo.finish(finished.id, SerperMapJobStatus.FINISHED)
    assert await repo.find_unfinished("k1") is None

    failed = await repo.create_job("j", {}, "k2", PAGES[:1], 1)
    await repo.finish(failed.id, SerperMapJobStatus.FAILED, "boom")
    assert await repo.find_unfinished("k2") is None


async def test_find_unfinished_returns_the_newest(db):
    repo = SerperMapRepository()
    await repo.create_job("old", {}, "k1", PAGES[:1], 1)
    newest = await repo.create_job("new", {}, "k1", PAGES[:1], 1)
    assert (await repo.find_unfinished("k1")).id == newest.id


async def test_pending_pages_returns_pending_and_failed_and_keeps_order(db):
    repo = SerperMapRepository()
    job = await repo.create_job("j", {}, "k1", PAGES, 1)
    await repo.mark_done(
        job.id, PAGES[0], http_status=200, places=20, credits=3, key_hint=0, saved=18
    )
    await repo.mark_failed(job.id, PAGES[1], http_status=500, error="boom")
    pages = await repo.pending_pages(job.id)
    assert pages == [PAGES[1], PAGES[2]]


async def test_mark_done_updates_the_job_counters(db):
    repo = SerperMapRepository()
    job = await repo.create_job("j", {}, "k1", PAGES, 1)
    await repo.mark_done(
        job.id, PAGES[0], http_status=200, places=20, credits=3, key_hint=0, saved=7
    )
    stored = await repo.get(job.id)
    assert stored.done_requests == 1
    assert stored.places_found == 20
    assert stored.places_saved == 7
    assert stored.credits_spent == 3
    assert stored.pending_requests == 2


async def test_mark_attempt_counts_calls_and_remembers_the_key_index(db):
    repo = SerperMapRepository()
    job = await repo.create_job("j", {}, "k1", PAGES[:1], 2)
    await repo.mark_attempt(job.id, PAGES[0], 1)
    request = await SerperMapRequestModel.get(job_id=job.id)
    assert request.attempts == 1 and request.key_hint == 1


async def test_finish_sets_status_and_error(db):
    repo = SerperMapRepository()
    job = await repo.create_job("j", {}, "k1", PAGES[:1], 1)
    await repo.finish(job.id, SerperMapJobStatus.EXHAUSTED, "all keys are out of credits")
    stored = await repo.get(job.id)
    assert stored.status == "exhausted"
    assert stored.error == "all keys are out of credits"


async def test_an_unknown_job_reads_as_none(db):
    assert await SerperMapRepository().get(uuid4()) is None
