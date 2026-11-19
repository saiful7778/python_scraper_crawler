import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast

import pytest

from app.services.browser import BrowserSession
from app.services.google_map_scraper.scrape import review_pass as review_module
from app.services.google_map_scraper.scrape.review_pass import ReviewPass
from app.services.google_map_scraper.scrape.scrape_progress import ScrapeProgress
from app.services.google_map_scraper.scrape.scrape_settings import ScrapeSettings
from app.services.google_map_scraper.scrape.scrape_types import LeadRow


class FakeTab:
    url = "https://www.google.com/maps/search/"

    def set_secure_cookie(self, name, value, domain):
        pass

    def get_url(self):
        return self.url


class FakeBrowser:
    @contextmanager
    def open_tab(self, proxy):
        yield SimpleNamespace(tab=FakeTab())


class FakeProgress:
    def save(self, **kwargs):
        pass


def _rows(count: int) -> list[LeadRow]:
    return [
        cast(
            LeadRow,
            {
                "place_id": f"P{i}",
                "link": f"https://www.google.com/maps/search/?x={i}",
                "title": f"T{i}",
            },
        )
        for i in range(count)
    ]


def _review_pass(notes: list[str]) -> ReviewPass:
    settings = SimpleNamespace(language="en", country="au")
    return ReviewPass(
        browser=cast(BrowserSession, FakeBrowser()),
        settings=cast(ScrapeSettings, settings),
        deadline=time.monotonic() + 60,
        stop=threading.Event(),
        proxies=[],
        progress=cast(ScrapeProgress, FakeProgress()),
        notes=notes,
    )


def test_stops_after_three_empty_panels_in_a_row(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def empty_panel(tab, url, timeout=10.0):
        calls.append(url)
        return ""

    monkeypatch.setattr(review_module, "fetch_lead_panel_json", empty_panel)
    notes: list[str] = []

    reviewed = _review_pass(notes).run(_rows(10))

    assert reviewed == 0
    assert len(calls) == 3
    assert notes == ["reviews read for 0 of 10 leads: 3 panels in a row were empty"]


def test_an_empty_panel_resets_the_count(monkeypatch: pytest.MonkeyPatch):
    bodies = iter(["", "", "[]", "", "", "[]"])
    monkeypatch.setattr(
        review_module,
        "fetch_lead_panel_json",
        lambda tab, url, timeout=10.0: next(bodies),
    )
    monkeypatch.setattr(review_module, "parse_place_panel", lambda body: {})

    notes: list[str] = []
    reviewed = _review_pass(notes).run(_rows(6))

    assert reviewed == 2
    assert notes == []
