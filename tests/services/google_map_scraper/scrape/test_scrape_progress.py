import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.services.buffer.client import BufferError
from app.services.buffer.scrape_state import ScrapeState
from app.services.google_map_scraper.scrape.scrape_progress import ScrapeProgress


def progress_of(state, rows=(), offset=0):
    return ScrapeProgress(state, "s1", {}, list(rows), 0.0, 0.0, offset=offset)


def test_saved_progress_counts_leads_found_before_a_resume(fake_redis):
    state = ScrapeState(fake_redis, 3600)
    progress = progress_of(state, rows=[{"title": "A"}, {"title": "B"}], offset=5)

    progress.save(stage="search")

    assert state.get_progress("s1")["leads"] == 7


def test_a_redis_failure_while_saving_progress_names_redis(fake_redis):
    class Unreachable(ScrapeState):
        def save_progress(self, scrape_id, progress):
            raise RedisConnectionError("Connection refused")

    progress = progress_of(Unreachable(fake_redis, 3600))

    with pytest.raises(BufferError, match="Redis"):
        progress.save(stage="search")
