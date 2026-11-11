import json

from app.services.buffer.keys import (
    active_key,
    processing_key,
    queue_key,
    queued_key,
)
from app.services.buffer.website_queue import WebsiteJob, WebsiteQueue

TTL = 3600


def make_job(position: int = 0, website: str = "https://a.com") -> WebsiteJob:
    return {
        "scrape_id": "s1",
        "position": position,
        "row_id": str(position + 1),
        "website": website,
        "origin": website,
    }


def test_same_website_enqueued_once(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    job = make_job()
    assert queue.enqueue("s1", job) is True
    assert queue.enqueue("s1", {**job, "position": 1}) is False
    assert fake_redis.llen(queue_key("s1")) == 1


def test_enqueue_sets_ttl_on_new_keys(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    queue.enqueue("s1", make_job())
    assert 0 < fake_redis.ttl(queue_key("s1")) <= TTL
    assert 0 < fake_redis.ttl(queued_key("s1")) <= TTL


def test_claim_moves_job_to_processing_in_order(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    first = make_job(0, "https://a.com")
    second = make_job(1, "https://b.com")
    queue.enqueue("s1", first)
    queue.enqueue("s1", second)

    assert queue.claim("s1") == first
    assert fake_redis.llen(queue_key("s1")) == 1
    assert fake_redis.lrange(processing_key("s1"), 0, -1) == [json.dumps(first)]
    assert 0 < fake_redis.ttl(processing_key("s1")) <= TTL


def test_claim_returns_none_when_queue_is_empty(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    assert queue.claim("s1") is None


def test_each_job_is_handed_out_once_across_claims(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    for i in range(25):
        queue.enqueue("s1", make_job(i, f"https://site{i}.example"))

    claimed = [queue.claim("s1") for _ in range(25)]

    assert len({job["position"] for job in claimed}) == 25
    assert queue.claim("s1") is None
    assert queue.pending("s1") == 25


def test_complete_removes_job_from_processing(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    queue.enqueue("s1", make_job())
    claimed = queue.claim("s1")
    queue.complete("s1", claimed)
    assert fake_redis.llen(processing_key("s1")) == 0
    assert queue.pending("s1") == 0


def test_requeue_returns_claimed_job(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    queue.enqueue("s1", make_job())
    claimed = queue.claim("s1")
    assert queue.requeue_processing("s1") == 1
    assert queue.claim("s1") == claimed


def test_requeue_preserves_order_ahead_of_unclaimed_jobs(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    jobs = [make_job(i, f"https://site{i}.com") for i in range(3)]
    for job in jobs:
        queue.enqueue("s1", job)
    queue.claim("s1")
    queue.claim("s1")

    assert queue.requeue_processing("s1") == 2
    assert [queue.claim("s1") for _ in range(3)] == jobs


def test_requeue_with_nothing_processing_returns_zero(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    assert queue.requeue_processing("s1") == 0


def test_pending_counts_queue_and_processing(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    queue.enqueue("s1", make_job(0, "https://a.com"))
    queue.enqueue("s1", make_job(1, "https://b.com"))
    queue.claim("s1")
    assert queue.pending("s1") == 2


def test_discard_deletes_queue_keys(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    queue.enqueue("s1", make_job(0, "https://a.com"))
    queue.enqueue("s1", make_job(1, "https://b.com"))
    queue.claim("s1")
    queue.discard("s1")
    assert not fake_redis.exists(
        queue_key("s1"), processing_key("s1"), queued_key("s1")
    )


def test_scrape_ids_with_work_lists_active_scrapes_with_jobs(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    fake_redis.sadd(active_key(), "s1", "s2", "s3")
    queue.enqueue("s1", make_job())
    queue.enqueue("s2", make_job())
    queue.claim("s2")

    assert sorted(queue.scrape_ids_with_work()) == ["s1", "s2"]


def test_pending_jobs_lists_queued_and_claimed_jobs_without_removing_them(fake_redis):
    queue = WebsiteQueue(fake_redis, ttl_seconds=TTL)
    queue.enqueue("s1", make_job(0, "https://a.com"))
    queue.enqueue("s1", make_job(1, "https://b.com"))
    queue.claim("s1")

    jobs = queue.pending_jobs("s1")
    assert sorted(job["website"] for job in jobs) == ["https://a.com", "https://b.com"]
    assert queue.pending("s1") == 2
