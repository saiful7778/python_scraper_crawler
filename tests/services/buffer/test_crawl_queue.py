from app.services.buffer.crawl_queue import CrawlJob, CrawlQueue
from app.services.buffer.keys import (
    crawl_processing_key,
    crawl_queue_key,
    crawl_queued_key,
)

TTL = 3600


def make_job(website: str = "https://a.com", origin: str | None = None) -> CrawlJob:
    return {"website": website, "origin": origin or website}


def test_jobs_are_handed_out_first_in_first_out(fake_redis):
    queue = CrawlQueue(fake_redis, ttl_seconds=TTL)
    first = make_job("https://a.com")
    second = make_job("https://b.com")
    third = make_job("https://c.com")
    assert queue.enqueue(first) is True
    assert queue.enqueue(second) is True
    assert queue.enqueue(third) is True

    assert queue.claim() == first
    assert queue.claim() == second
    assert queue.claim() == third
    assert queue.claim() is None


def test_same_origin_enqueued_once(fake_redis):
    queue = CrawlQueue(fake_redis, ttl_seconds=TTL)
    job = make_job("https://a.com/page")
    assert queue.enqueue(job) is True
    assert queue.enqueue({**job, "website": "https://a.com/other"}) is False
    assert fake_redis.llen(crawl_queue_key()) == 1


def test_origin_can_be_enqueued_again_after_completion(fake_redis):
    queue = CrawlQueue(fake_redis, ttl_seconds=TTL)
    job = make_job()
    queue.enqueue(job)
    queue.complete(queue.claim())
    assert queue.enqueue(job) is True


def test_origin_is_rejected_while_being_crawled(fake_redis):
    queue = CrawlQueue(fake_redis, ttl_seconds=TTL)
    job = make_job()
    queue.enqueue(job)
    queue.claim()
    assert queue.enqueue(job) is False


def test_enqueue_sets_ttl_on_new_keys(fake_redis):
    queue = CrawlQueue(fake_redis, ttl_seconds=TTL)
    queue.enqueue(make_job())
    assert 0 < fake_redis.ttl(crawl_queue_key()) <= TTL
    assert 0 < fake_redis.ttl(crawl_queued_key()) <= TTL


def test_claim_moves_job_to_processing(fake_redis):
    queue = CrawlQueue(fake_redis, ttl_seconds=TTL)
    job = make_job()
    queue.enqueue(job)
    assert queue.claim() == job
    assert fake_redis.llen(crawl_queue_key()) == 0
    assert fake_redis.llen(crawl_processing_key()) == 1
    assert 0 < fake_redis.ttl(crawl_processing_key()) <= TTL


def test_complete_removes_job_from_processing_and_origin_set(fake_redis):
    queue = CrawlQueue(fake_redis, ttl_seconds=TTL)
    job = make_job()
    queue.enqueue(job)
    queue.complete(queue.claim())
    assert fake_redis.llen(crawl_processing_key()) == 0
    assert fake_redis.scard(crawl_queued_key()) == 0
    assert queue.pending() == 0


def test_requeue_returns_claimed_job_to_head(fake_redis):
    queue = CrawlQueue(fake_redis, ttl_seconds=TTL)
    first = make_job("https://a.com")
    second = make_job("https://b.com")
    queue.enqueue(first)
    queue.enqueue(second)
    queue.claim()
    assert queue.requeue_processing() == 1
    assert queue.claim() == first
    assert queue.claim() == second


def test_requeue_with_nothing_processing_returns_zero(fake_redis):
    queue = CrawlQueue(fake_redis, ttl_seconds=TTL)
    assert queue.requeue_processing() == 0


def test_pending_counts_queued_and_processing(fake_redis):
    queue = CrawlQueue(fake_redis, ttl_seconds=TTL)
    queue.enqueue(make_job("https://a.com"))
    queue.enqueue(make_job("https://b.com"))
    queue.claim()
    assert queue.pending() == 2


def test_queued_jobs_lists_unclaimed_jobs_in_order(fake_redis):
    queue = CrawlQueue(fake_redis, ttl_seconds=TTL)
    first = make_job("https://a.com")
    second = make_job("https://b.com")
    queue.enqueue(first)
    queue.enqueue(second)
    queue.claim()
    assert queue.queued_jobs() == [second]
