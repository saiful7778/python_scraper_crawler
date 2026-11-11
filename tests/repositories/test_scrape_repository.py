import re
from uuid import uuid4

from app.models import ScrapeModel
from app.repositories.scrape_repository import ScrapeRepository

SETTINGS = {"keywords": ["cafe"], "lang": "en", "max_time": 600}


async def test_claim_returns_oldest_pending_and_marks_it_working(db):
    repo = ScrapeRepository()
    await repo.create(uuid4(), "first", SETTINGS, ["cafe"])
    await repo.create(uuid4(), "second", SETTINGS, ["cafe"])

    claimed = await repo.claim_oldest_pending()

    assert claimed["Name"] == "first"
    assert claimed["Status"] == "working"
    assert (await repo.get(claimed["ID"]))["Status"] == "working"
    assert (await repo.claim_oldest_pending())["Name"] == "second"
    assert await repo.claim_oldest_pending() is None


async def test_create_and_get_return_the_scrape_record(db):
    repo = ScrapeRepository()
    scrape_id = uuid4()
    await repo.create(scrape_id, "cafe @ Austin", SETTINGS, ["cafe"])

    record = await repo.get(scrape_id)

    assert record["ID"] == str(scrape_id)
    assert record["Name"] == "cafe @ Austin"
    assert record["Status"] == "pending"
    assert record["Data"] == SETTINGS
    assert record["Error"] == ""
    assert record["Progress"] == {"resumes": 0}
    created_at = (await ScrapeModel.get(id=scrape_id)).created_at
    assert record["Date"] == created_at.isoformat(timespec="seconds")
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?!\.)", record["Date"])
    assert await repo.get(uuid4()) is None


async def test_list_all_returns_newest_first(db):
    repo = ScrapeRepository()
    await repo.create(uuid4(), "first", SETTINGS, ["cafe"])
    await repo.create(uuid4(), "second", SETTINGS, ["cafe"])

    assert [r["Name"] for r in await repo.list_all()] == ["second", "first"]


async def test_set_status_truncates_the_error(db):
    repo = ScrapeRepository()
    scrape_id = uuid4()
    await repo.create(scrape_id, "first", SETTINGS, ["cafe"])

    await repo.set_status(scrape_id, "failed", "x" * 600)

    record = await repo.get(scrape_id)
    assert record["Status"] == "failed"
    assert record["Error"] == "x" * 500


async def test_save_progress_merges_into_existing_progress(db):
    repo = ScrapeRepository()
    scrape_id = uuid4()
    await repo.create(scrape_id, "first", SETTINGS, ["cafe"])

    await repo.save_progress(scrape_id, {"stage": "search", "done": 1})
    await repo.save_progress(scrape_id, {"done": 2})

    progress = (await repo.get(scrape_id))["Progress"]
    assert progress["stage"] == "search"
    assert progress["done"] == 2


async def test_finish_stores_the_outcome_and_reports_a_deleted_scrape(db):
    repo = ScrapeRepository()
    scrape_id = uuid4()
    await repo.create(scrape_id, "first", SETTINGS, ["cafe"])

    assert await repo.finish(scrape_id, "ok", "", {"stage": "done", "percent": 100})

    record = await repo.get(scrape_id)
    assert record["Status"] == "ok"
    assert record["Progress"]["percent"] == 100
    assert not await repo.finish(uuid4(), "ok", "", {})


async def test_delete_reports_whether_the_scrape_existed(db):
    repo = ScrapeRepository()
    scrape_id = uuid4()
    await repo.create(scrape_id, "first", SETTINGS, ["cafe"])

    assert await repo.delete(scrape_id)
    assert not await repo.delete(scrape_id)
    assert await repo.get(scrape_id) is None


async def test_requeue_resumes_working_scrapes_under_the_limit(db):
    repo = ScrapeRepository()
    scrape_id = uuid4()
    await repo.create(scrape_id, "first", SETTINGS, ["cafe"])
    await repo.claim_oldest_pending()

    assert await repo.requeue_interrupted(max_resumes=3) == (1, 0)

    record = await repo.get(scrape_id)
    assert record["Status"] == "pending"
    assert record["Progress"]["resumes"] == 1


async def test_requeue_fails_scrape_after_max_resumes(db):
    repo = ScrapeRepository()
    scrape_id = uuid4()
    await repo.create(scrape_id, "first", SETTINGS, ["cafe"])
    await repo.claim_oldest_pending()
    await ScrapeModel.filter(id=scrape_id).update(resumes=3)

    assert await repo.requeue_interrupted(max_resumes=3) == (0, 1)

    record = await repo.get(scrape_id)
    assert record["Status"] == "failed"
    assert "4 times" in record["Error"]


async def test_requeue_ignores_scrapes_that_are_not_working(db):
    repo = ScrapeRepository()
    await repo.create(uuid4(), "first", SETTINGS, ["cafe"])

    assert await repo.requeue_interrupted(max_resumes=3) == (0, 0)
