import asyncio

import pytest

from app.repositories.serper_map_repository import SerperMapPage
from app.services.serper.maps.serper_map_client import SerperMapCaller
from app.services.serper.maps.serper_map_errors import (
    SerperAuthError,
    SerperConfigError,
    SerperNoCreditsError,
    SerperRateLimitedError,
    SerperUnavailableError,
)
from app.services.serper.maps.serper_map_state import (
    KEY_DEAD,
    KEY_READY,
    SerperMapScheduler,
)
from app.services.serper.maps.serper_map_type import SearchResult


class FakeClient:
    """A client whose search() records calls and answers from a script."""

    def __init__(self, index, answers):
        self.index = index
        self.answers = list(answers)
        self.calls = []

    async def search(self, keyword, lat, lon, zoom, page):
        self.calls.append((keyword, page))
        answer = self.answers.pop(0) if self.answers else SearchResult(ll="", places=[], credits=3)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def balance(self) -> int:
        return 100

    async def aclose(self):
        pass


def _pages(n):
    return [SerperMapPage("k", page, 0, 0, 30.0, -97.0, 13) for page in range(1, n + 1)]


async def test_every_page_is_handled_once():
    handled = []

    async def handle(page, result, key):
        handled.append(page.page)
        return 1

    summary = await SerperMapScheduler(
        _pages(6), [FakeClient(0, [])], parallel=2, rest_seconds=0
    ).run(handle)
    assert sorted(handled) == [1, 2, 3, 4, 5, 6]
    assert summary.done == 6 and summary.failed == 0 and summary.pending == 0


async def test_each_key_runs_parallel_requests_at_a_time():
    active = {0: 0, 1: 0}
    peak = {0: 0, 1: 0}

    class Slow(FakeClient):
        async def search(self, keyword, lat, lon, zoom, page):
            active[self.index] += 1
            peak[self.index] = max(peak[self.index], active[self.index])
            await asyncio.sleep(0.02)
            active[self.index] -= 1
            return SearchResult(ll="", places=[], credits=3)

    async def handle(page, result, key):
        return 0

    await SerperMapScheduler(
        _pages(20), [Slow(0, []), Slow(1, [])], parallel=3, rest_seconds=0
    ).run(handle)
    assert peak == {0: 3, 1: 3}


async def test_one_dry_key_hands_its_work_to_the_others():
    dry = FakeClient(0, [SerperNoCreditsError("out")] * 10)
    healthy = FakeClient(1, [])
    handled = []

    async def handle(page, result, key):
        handled.append(key)
        return 0

    summary = await SerperMapScheduler(
        _pages(10), [dry, healthy], parallel=2, rest_seconds=0
    ).run(handle)
    assert summary.done == 10
    assert dry.calls[0] == ("k", 1)
    assert sum(1 for key in handled if key == 0) <= 2  # only the in-flight ones
    assert summary.exhausted is False
    assert summary.dry_keys == 1


async def test_all_keys_dry_stops_with_pending_work_and_is_not_a_failure():
    dry = [FakeClient(i, [SerperNoCreditsError("out")] * 10) for i in range(2)]

    async def handle(page, result, key):
        return 0

    summary = await SerperMapScheduler(
        _pages(10), list[SerperMapCaller](dry), parallel=2, rest_seconds=0
    ).run(handle)
    assert summary.exhausted is True
    assert summary.done == 0
    assert summary.pending == 10
    assert summary.failed == 0


async def test_an_auth_error_retires_the_key():
    bad = FakeClient(0, [SerperAuthError("403: Unauthorized")] * 5)
    good = FakeClient(1, [])

    async def handle(page, result, key):
        return 0

    summary = await SerperMapScheduler(
        _pages(4), [bad, good], parallel=1, rest_seconds=0
    ).run(handle)
    assert summary.done == 4
    assert len(bad.calls) == 1
    assert summary.dead_keys == 1
    assert summary.pending == 0


async def test_a_rate_limited_key_waits_and_retries_itself():
    limited = FakeClient(0, [SerperRateLimitedError("429", retry_after=0)])
    healthy = FakeClient(1, [])

    async def handle(page, result, key):
        return 0

    scheduler = SerperMapScheduler(
        _pages(2), [limited, healthy], parallel=1, rest_seconds=0
    )
    summary = await scheduler.run(handle)
    assert summary.done == 2
    assert scheduler.slots[0].state == KEY_READY
    assert len(limited.calls) == 2  # the failed page, then the next


async def test_repeated_rate_limits_retire_the_key_without_failing_the_page():
    limited = FakeClient(0, [SerperRateLimitedError("429", retry_after=0)] * 20)
    healthy = FakeClient(1, [])

    async def handle(page, result, key):
        return 0

    scheduler = SerperMapScheduler(
        _pages(4), [limited, healthy], parallel=1, rest_seconds=0
    )
    summary = await scheduler.run(handle)
    assert summary.done == 4
    assert summary.failed == 0
    assert scheduler.slots[0].state == KEY_DEAD


async def test_a_transient_failure_is_retried_then_marked_failed():
    flaky = FakeClient(0, [SerperUnavailableError("500")] * 3)
    failures = []

    async def handle(page, result, key):
        return 0

    async def on_failure(page, status, error):
        failures.append((page.page, error))

    summary = await SerperMapScheduler(
        _pages(1), [flaky], parallel=1, rest_seconds=0, on_failure=on_failure
    ).run(handle)
    assert summary.failed == 1
    assert summary.done == 0
    assert failures == [(1, "SerperUnavailableError: 500")]


async def test_a_transient_failure_recovers_on_a_later_try():
    flaky = FakeClient(
        0, [SerperUnavailableError("500"), SearchResult(ll="", places=[], credits=3)]
    )

    async def handle(page, result, key):
        return 1

    summary = await SerperMapScheduler(
        _pages(1), [flaky], parallel=1, rest_seconds=0
    ).run(handle)
    assert summary.done == 1 and summary.failed == 0


async def test_a_page_that_raises_in_the_handler_does_not_stop_the_run():
    calls = []

    async def handle(page, result, key):
        calls.append(page.page)
        if page.page == 1:
            raise RuntimeError("save failed")
        return 1

    summary = await SerperMapScheduler(
        _pages(3), [FakeClient(0, [])], parallel=1, rest_seconds=0
    ).run(handle)
    assert sorted(calls) == [1, 2, 3]
    assert summary.done == 2 and summary.failed == 1


async def test_the_rest_happens_between_batches(monkeypatch):
    slept = []

    async def fake_sleep(self, seconds):
        slept.append(seconds)
        await asyncio.sleep(0)

    async def handle(page, result, key):
        return 0

    monkeypatch.setattr(SerperMapScheduler, "_sleep", fake_sleep)
    await SerperMapScheduler(
        _pages(6), [FakeClient(0, [])], parallel=2, rest_seconds=1.5
    ).run(handle)
    assert slept == [1.5, 1.5]


async def test_a_cancelled_run_returns_its_pages_to_the_queue():
    cancelled = asyncio.Event()

    class Slow(FakeClient):
        async def search(self, keyword, lat, lon, zoom, page):
            await asyncio.sleep(0)
            return SearchResult(ll="", places=[], credits=3)

    async def handle(page, result, key):
        cancelled.set()  # cancel while the first page is being saved
        return 0

    summary = await SerperMapScheduler(
        _pages(5), [Slow(0, [])], parallel=1, rest_seconds=0, cancelled=cancelled
    ).run(handle)
    assert summary.done == 1
    assert summary.pending == 4


async def test_events_report_progress():
    events = []

    async def handle(page, result, key):
        return 2

    await SerperMapScheduler(
        _pages(2),
        [FakeClient(0, [])],
        parallel=1,
        rest_seconds=0,
        on_event=events.append,
    ).run(handle)
    kinds = [event.kind for event in events]
    assert "request_done" in kinds and kinds[-1] == "finished"
    assert events[-1].done == 2 and events[-1].total == 2


async def test_a_slow_page_on_the_last_batch_is_not_counted_as_pending():
    class Slow(FakeClient):
        async def search(self, keyword, lat, lon, zoom, page):
            await asyncio.sleep(0.02)
            return SearchResult(ll="", places=[], credits=3)

    async def handle(page, result, key):
        return 0

    summary = await SerperMapScheduler(
        _pages(3), [Slow(0, []), Slow(1, [])], parallel=2, rest_seconds=0
    ).run(handle)
    assert summary.done == 3
    assert summary.pending == 0
    assert summary.exhausted is False


async def test_no_keys_is_a_config_error_not_an_exhausted_run():
    async def handle(page, result, key):
        return 0

    with pytest.raises(SerperConfigError):
        await SerperMapScheduler(_pages(3), [], parallel=1).run(handle)
