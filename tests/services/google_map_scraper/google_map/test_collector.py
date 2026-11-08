from typing import Any, cast

import pytest

from app.services.browser import BrowserTab
from app.services.browser.cdp_error import CDPError
from app.services.google_map_scraper.google_map import (
    CONSENT_COOKIES,
    JS_DISMISS_CONSENT,
    JS_FEED_END_STATE,
    JS_FEED_LINKS,
    JS_FEED_PRESENT,
    JS_SCROLL_FEED_TO_BOTTOM,
    collect_leads,
)
from tests.services.google_map_scraper.google_map.fakes import (
    FakeClock,
    FakeTab,
    Script,
)

URL = "https://www.google.com/maps/search/cafe?hl=en"
SORRY_URL = "https://www.google.com/sorry/index?continue=x"
CONSENT_URL = "https://consent.google.com/m?continue=x"
PLACE_URL = "https://www.google.com/maps/place/Cafe/data=!3d40.7!4d-74.0!19sChIJabc"

ROW_A = {"data_id": "0x1:0x2", "title": "Alpha"}
ROW_B = {"data_id": "0x3:0x4", "title": "Beta"}


class FakeBatchReader:
    """Replays one step per poll(): ``batch``, ``ask`` or ``limited`` below."""

    def __init__(self, tab: FakeTab, steps: list[Any]) -> None:
        self.tab = tab
        self.steps: list[Any] = list(steps)
        self.count = 0
        self.asked = 0
        self.limited = 0
        self.inflight = 0
        self.pending = 0
        self.closed = False

    def poll(self) -> list[dict[str, Any]]:
        if not self.steps:
            return []
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        self.asked += step["asked"]
        self.limited += step["limited"]
        if step["rows"] is None:
            return []
        self.count += 1
        return step["rows"]

    def close(self) -> None:
        self.closed = True


def batch(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"rows": list(rows), "asked": 0, "limited": 0}


def ask() -> dict[str, Any]:
    return {"rows": None, "asked": 1, "limited": 0}


def limited() -> dict[str, Any]:
    return {"rows": None, "asked": 0, "limited": 1}


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch):
    """Run collect_leads over scripted batch steps; returns (result, reader)."""
    clock = FakeClock()
    monkeypatch.setattr(
        "app.services.google_map_scraper.google_map.collector.time", clock
    )

    def _run(tab: FakeTab, steps: list[Any], **kwargs: Any):
        readers: list[FakeBatchReader] = []

        def factory(tab: FakeTab) -> FakeBatchReader:
            reader = FakeBatchReader(tab, steps)
            readers.append(reader)
            return reader

        monkeypatch.setattr(
            "app.services.google_map_scraper.google_map.collector.BatchResponseReader",
            factory,
        )
        kwargs.setdefault("log", lambda message: None)
        result = collect_leads(cast(BrowserTab, tab), URL, **kwargs)
        return result, readers[0]

    return _run


def test_installs_consent_cookies_on_google_domain(run):
    tab = FakeTab()

    run(tab, [batch(ROW_A)], depth=1)

    assert tab.cookies == [
        (name, value, ".google.com") for name, value in CONSENT_COOKIES.items()
    ]


def test_collects_batches_until_depth_is_reached(run):
    tab = FakeTab()
    tab.evaluations[JS_FEED_LINKS] = Script([])

    (leads, reason, stats), reader = run(tab, [batch(ROW_A), batch(ROW_B)], depth=2)

    assert leads == [ROW_A, ROW_B]
    assert reason == "depth"
    assert stats == {"batches": 2, "dom_links": 0}
    assert tab.navigated == [URL]
    assert reader.closed


def test_deduplicates_leads_across_batches_and_reports_only_new_rows(run):
    tab = FakeTab()
    seen_batches: list[tuple[list[Any], int]] = []
    renamed_a = {"data_id": "0x1:0x2", "title": "Alpha renamed"}

    (leads, _, stats), _ = run(
        tab,
        [batch(ROW_A, ROW_B), batch(renamed_a)],
        depth=2,
        on_batch=lambda rows, count: seen_batches.append((rows, count)),
    )

    assert leads == [ROW_A, ROW_B]
    assert seen_batches == [([ROW_A, ROW_B], 1)]
    assert stats["batches"] == 2


def test_stops_at_end_of_list_marker(run):
    tab = FakeTab()
    tab.evaluations[JS_FEED_END_STATE] = Script({"cls": True, "text": ""})

    (leads, reason, stats), _ = run(tab, [batch(ROW_A)], depth=5)

    assert leads == [ROW_A]
    assert reason == "end-of-list"
    assert stats == {"batches": 1, "dom_links": 0}


def test_scrolls_the_feed_to_request_the_next_batch(run):
    tab = FakeTab()
    tab.evaluations[JS_FEED_LINKS] = Script([])

    run(tab, [batch(ROW_A), batch(ROW_B)], depth=2)

    assert JS_SCROLL_FEED_TO_BOTTOM in tab.evaluated


def test_returns_blocked_when_google_serves_sorry_page(run):
    tab = FakeTab()
    tab.redirect_to = SORRY_URL

    result, reader = run(tab, [batch(ROW_A)])

    assert result == ([], "blocked", {"batches": 0, "dom_links": 0})
    assert reader.closed


def test_dismisses_consent_interstitial_then_continues(run):
    tab = FakeTab()
    tab.redirect_to = CONSENT_URL
    tab.evaluations[JS_DISMISS_CONSENT] = Script(True)

    (leads, reason, _), _ = run(tab, [batch(ROW_A)], depth=1)

    assert JS_DISMISS_CONSENT in tab.evaluated
    assert leads == [ROW_A]
    assert reason == "depth"


def test_returns_single_place_when_query_resolves_to_one_place(run):
    tab = FakeTab()
    tab.redirect_to = PLACE_URL
    tab.truthy[JS_FEED_PRESENT] = Script(None)

    (leads, reason, stats), reader = run(tab, [])

    assert reason == "single"
    assert stats == {"batches": 0, "dom_links": 1}
    assert len(leads) == 1
    assert leads[0]["place_id"] == "ChIJabc"
    assert leads[0]["latitude"] == "40.7"
    assert leads[0]["longitude"] == "-74.0"
    assert reader.closed


def test_returns_no_feed_when_page_is_neither_list_nor_place(run):
    tab = FakeTab()
    tab.truthy[JS_FEED_PRESENT] = Script(None)

    result, _ = run(tab, [])

    assert result == ([], "no-feed", {"batches": 0, "dom_links": 0})


def test_blocked_when_google_refuses_next_batch(run):
    tab = FakeTab()
    tab.evaluations[JS_FEED_LINKS] = Script([])

    result, _ = run(tab, [limited()])

    assert result == ([], "blocked", {"batches": 0, "dom_links": 0})


def test_falls_back_to_dom_links_when_no_batch_was_read(run):
    tab = FakeTab()
    alpha = {
        "href": "https://www.google.com/maps/place/Alpha/data=!1s0x1:0x2",
        "label": "",
    }
    beta = {
        "href": "https://www.google.com/maps/place/Beta/data=!1s0x3:0x4",
        "label": "Beta cafe",
    }
    tab.evaluations[JS_FEED_LINKS] = Script([alpha, alpha, beta])

    (leads, reason, stats), _ = run(tab, [])

    assert [lead["title"] for lead in leads] == ["Alpha", "Beta cafe"]
    assert [lead["data_id"] for lead in leads] == ["0x1:0x2", "0x3:0x4"]
    assert reason == "end-of-list+dom-fallback"
    assert stats == {"batches": 0, "dom_links": 3}


def test_returns_leads_gathered_before_a_mid_scroll_error(run):
    tab = FakeTab()

    result, reader = run(tab, [batch(ROW_A), CDPError("boom")], depth=5)

    assert result == (
        [ROW_A],
        "error: CDPError: boom",
        {"batches": 1, "dom_links": 0},
    )
    assert reader.closed


def test_reraises_error_when_nothing_was_collected(run):
    tab = FakeTab()

    with pytest.raises(CDPError, match="gone"):
        run(tab, [CDPError("gone")])


def test_stops_when_stop_callback_fires(run):
    tab = FakeTab()

    (leads, reason, _), _ = run(tab, [batch(ROW_A)], depth=5, stop=lambda: True)

    assert leads == [ROW_A]
    assert reason == "stopped"


def test_reports_timeout_when_wall_clock_budget_is_spent(run):
    tab = FakeTab()

    (leads, reason, _), _ = run(tab, [batch(ROW_A)], depth=5, max_seconds=0.0)

    assert leads == [ROW_A]
    assert reason == "timeout"


def test_logs_progress_for_each_batch(run):
    tab = FakeTab()
    messages: list[str] = []

    run(
        tab,
        [batch(ROW_A), batch(ROW_B)],
        depth=2,
        log=messages.append,
    )

    assert messages == ["  batch 1: 1 leads", "  batch 2: 2 leads"]
