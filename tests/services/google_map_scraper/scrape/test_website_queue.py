import time

import pytest

from app.helpers.http_fetcher.models import FetchResult, build_result
from app.services.crawler.storage.crawl_store import CrawlStore
from app.services.crawler.storage.postgres_sink import PostgresSink
from app.services.crawler.worker import CrawlerWorker
from app.services.google_map_scraper.scrape.scrape_store import ScrapeStore
from app.services.google_map_scraper.scrape.scrape_types import LeadRow, SettingsData

PAGE = '<a href="mailto:hi@bakery.studio.de">mail</a>'


def _fetch_page(page: str):
    def fetch(url: str) -> FetchResult:
        if url.rstrip("/") == "https://bakery.studio.de":
            return build_result(
                trail=[url],
                status=200,
                headers={"content-type": "text/html"},
                body=page.encode(),
            )
        return build_result(trail=[url], status=404)

    return fetch


def _settings() -> SettingsData:
    return {  # type: ignore[return-value]
        "keywords": ["bakery"],
        "lang": "de",
        "max_time": 600,
        "email": True,
        "socials": False,
    }


def _lead(title: str, **extra: str) -> LeadRow:
    row: dict[str, object] = {key: "" for key in LeadRow.__required_keys__}
    row["latitude"] = None
    row["longitude"] = None
    row["title"] = title
    row.update(extra)
    return row  # type: ignore[return-value]


@pytest.fixture
def store(tmp_path):
    scrape_store = ScrapeStore(str(tmp_path))
    yield scrape_store
    scrape_store.close()


def _scrape_with_two_leads(store: ScrapeStore) -> str:
    scrape_id = store.create_scrape("nightly", _settings())
    store.append_rows(
        scrape_id,
        [_lead("Bakery", website="https://bakery.studio.de"), _lead("No site")],
    )
    return scrape_id


def test_only_leads_with_a_website_are_queued(store):
    scrape_id = _scrape_with_two_leads(store)
    assert CrawlStore(store).queue_websites(scrape_id) == 1
    assert CrawlStore(store).count_pending_websites(scrape_id) == 1


def test_queueing_twice_does_not_requeue_a_lead(store):
    scrape_id = _scrape_with_two_leads(store)
    CrawlStore(store).queue_websites(scrape_id)
    assert CrawlStore(store).queue_websites(scrape_id) == 0


def test_next_batch_carries_the_scrape_options(store):
    scrape_id = _scrape_with_two_leads(store)
    CrawlStore(store).queue_websites(scrape_id)
    batch = CrawlStore(store).next_website_batch(8)
    assert batch is not None
    assert batch.scrape_id == scrape_id
    assert batch.collect_emails is True
    assert batch.collect_socials is False
    assert [target.website for target in batch.websites] == ["https://bakery.studio.de"]


def test_skipped_lead_ignores_a_late_result(store):
    scrape_id = _scrape_with_two_leads(store)
    CrawlStore(store).queue_websites(scrape_id)
    batch = CrawlStore(store).next_website_batch(8)
    assert batch is not None
    position = batch.websites[0].position

    assert CrawlStore(store).skip_pending_websites(scrape_id) == 1
    CrawlStore(store).save_website_result(
        scrape_id, position, {"emails": "hi@bakery.studio.de"}
    )

    assert CrawlStore(store).count_pending_websites(scrape_id) == 0
    assert store.read_rows(scrape_id)[0]["emails"] == ""


def test_crawler_worker_stores_what_it_finds(store):
    scrape_id = _scrape_with_two_leads(store)
    CrawlStore(store).queue_websites(scrape_id)
    worker = CrawlerWorker(
        CrawlStore(store), fetch=_fetch_page(PAGE), sink=PostgresSink(config=None)
    )
    worker.start()
    deadline = time.monotonic() + 10
    while (
        CrawlStore(store).count_pending_websites(scrape_id)
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    worker.shutdown(wait=2)

    rows = store.read_rows(scrape_id)
    assert rows[0]["emails"] == "hi@bakery.studio.de"
    assert CrawlStore(store).count_pending_websites(scrape_id) == 0


def test_concurrent_batches_claim_different_websites(store):
    scrape_id = store.create_scrape("nightly", _settings())
    store.append_rows(
        scrape_id,
        [_lead(f"Bakery {i}", website=f"https://site{i}.example") for i in range(25)],
    )
    CrawlStore(store).queue_websites(scrape_id)

    first = CrawlStore(store).next_website_batch(10)
    second = CrawlStore(store).next_website_batch(10)
    third = CrawlStore(store).next_website_batch(10)
    assert first is not None and second is not None and third is not None
    claimed = [t.position for b in (first, second, third) for t in b.websites]
    assert len(first.websites) == len(second.websites) == 10
    assert len(third.websites) == 5
    assert len(set(claimed)) == 25
    assert CrawlStore(store).next_website_batch(10) is None
    assert CrawlStore(store).count_pending_websites(scrape_id) == 25


def test_requeue_puts_claimed_websites_back_in_the_queue(store):
    scrape_id = _scrape_with_two_leads(store)
    CrawlStore(store).queue_websites(scrape_id)
    assert CrawlStore(store).next_website_batch(8) is not None
    assert CrawlStore(store).next_website_batch(8) is None

    assert CrawlStore(store).requeue_running_websites() == 1
    assert CrawlStore(store).next_website_batch(8) is not None
