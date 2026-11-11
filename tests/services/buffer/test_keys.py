from app.services.buffer.keys import (
    active_key,
    crawl_finished_key,
    crawl_key,
    crawl_owners_key,
    known_key,
    processing_key,
    queue_key,
    queued_key,
    rows_key,
    state_key,
)


def test_keys_are_namespaced_per_scrape():
    assert state_key("abc") == "scrape:abc:state"
    assert rows_key("abc") == "scrape:abc:rows"
    assert known_key("abc") == "scrape:abc:known"
    assert queue_key("abc") == "scrape:abc:queue"
    assert processing_key("abc") == "scrape:abc:processing"
    assert queued_key("abc") == "scrape:abc:queued"
    assert active_key() == "buffer:active"
    assert crawl_key("w1") == "buffer:crawl:w1"
    assert crawl_finished_key() == "buffer:crawl_finished"
    assert crawl_owners_key() == "buffer:crawl_owners"
