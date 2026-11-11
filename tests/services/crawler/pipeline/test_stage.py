import fakeredis
import pytest

from app.helpers.http_fetcher import FetchResult, SITE_TIMEOUT
from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.crawl_queue import CrawlQueue
from app.services.crawler.pipeline.intake import CrawlIntake, IntakeResult
from app.services.crawler.pipeline.stage import CRAWL_OPTIONS, CrawlStage, site_fetch

TTL = 3600
HTML = b"<html><head><title>Home</title></head><body>Bakery</body></html>"


def page(url, status=200, content_type="text/html; charset=utf-8"):
    return FetchResult(
        url=url,
        final_url=url,
        status=status,
        headers={"Content-Type": content_type},
        body=HTML if status == 200 else b"",
        redirect_chain=(),
        error=None,
    )


class Web:
    def __init__(self, dead=()):
        self.dead = set(dead)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        host = url.split("://", 1)[1].split("/", 1)[0]
        if host in self.dead:
            return page(url, status=404)
        return page(url)


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def queue(fake_redis):
    return CrawlQueue(fake_redis)


@pytest.fixture
def buffer(fake_redis):
    return RedisCrawlBuffer(fake_redis, TTL)


def test_submit_queues_crawlable_origins_in_input_order(queue, buffer):
    web = Web()
    intake = CrawlIntake(queue, buffer, web)

    result = intake.submit(["https://b.example/about", "https://a.example"])

    assert result == IntakeResult(queued=2, rejected=0, skipped=0)
    assert queue.queued_jobs() == [
        {"website": "https://b.example/about", "origin": "https://b.example"},
        {"website": "https://a.example", "origin": "https://a.example"},
    ]


def test_submit_records_non_crawlable_website_as_failed_and_queues_nothing(
    queue, buffer
):
    intake = CrawlIntake(queue, buffer, Web(dead={"dead.example"}))

    result = intake.submit(["https://dead.example/contact"])

    assert result == IntakeResult(queued=0, rejected=1, skipped=0)
    assert queue.queued_jobs() == []
    (website_id,) = buffer.finished_website_ids()
    snapshot = buffer.read_website(website_id)
    assert snapshot.website["origin"] == "https://dead.example"
    assert snapshot.website["status"] == "failed"
    assert snapshot.website["error_code"] == "not_crawlable"


def test_two_leads_with_one_origin_produce_one_job(queue, buffer):
    intake = CrawlIntake(queue, buffer, Web())

    result = intake.submit(["https://a.example/one", "https://a.example/two"])

    assert result == IntakeResult(queued=1, rejected=0, skipped=1)
    assert queue.queued_jobs() == [
        {"website": "https://a.example/one", "origin": "https://a.example"}
    ]
    assert buffer.finished_website_ids() == []


def test_submit_skips_websites_without_a_usable_origin(queue, buffer):
    intake = CrawlIntake(queue, buffer, Web())

    result = intake.submit(["", "   ", "https://ok.example"])

    assert result == IntakeResult(queued=1, rejected=0, skipped=2)
    assert [job["origin"] for job in queue.queued_jobs()] == ["https://ok.example"]


def test_submit_does_not_reset_a_website_already_in_the_buffer(queue, buffer):
    web = Web()
    website_id = buffer.create_website(
        host="a.example",
        registrable_domain="a.example",
        origin="https://a.example",
    )
    buffer.create_run(
        website_id=website_id, requested_url="https://a.example", config={}
    )
    intake = CrawlIntake(queue, buffer, web)

    result = intake.submit(["https://a.example/contact"])

    assert result == IntakeResult(queued=0, rejected=0, skipped=1)
    assert web.calls == []
    assert queue.queued_jobs() == []
    snapshot = buffer.read_website(website_id)
    assert len(snapshot.runs) == 1


def test_run_until_empty_checks_every_queued_job_and_empties_the_queue(
    queue, buffer
):
    intake = CrawlIntake(queue, buffer, Web())
    intake.submit(["https://a.example", "https://b.example"])

    stage = CrawlStage(queue, buffer, Web(), size=2, options=CRAWL_OPTIONS)
    stage.run_until_empty()

    assert queue.pending() == 0
    assert buffer.finished_count() == 2
    for website_id in buffer.finished_website_ids():
        assert buffer.read_website(website_id).website["status"] != "running"


def test_site_fetch_passes_the_site_timeout():
    class Fetcher:
        def __init__(self):
            self.timeouts = []

        def fetch_result(self, url, *, timeout=None):
            self.timeouts.append(timeout)
            return page(url)

    fetcher = Fetcher()
    site_fetch(fetcher)("https://a.example")

    assert fetcher.timeouts == [SITE_TIMEOUT]


def test_crawl_options_collect_emails_and_socials():
    assert CRAWL_OPTIONS.collect_emails is True
    assert CRAWL_OPTIONS.collect_socials is True
    assert CRAWL_OPTIONS.max_pages == 100
