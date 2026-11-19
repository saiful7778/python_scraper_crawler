from typing import cast

import fakeredis

from app.services.browser import BrowserSession, CDPError
from app.services.google_map_scraper.scrape.scrape_database import ScrapeDatabase
from app.services.google_map_scraper.scrape.scrape_run import ScrapeRun
from app.services.google_map_scraper.scrape.scrape_settings import ScrapeSettings
from app.services.google_map_scraper.scrape.scrape_types import (
    ScrapeRecord,
    SettingsData,
)

SCRAPE_ID = "s-conn"


class _StubBrowserSession:
    """Stands in for BrowserSession: a browser whose CDP connection can be closed."""

    def __init__(self):
        self.browser = None
        self.cdp_closed = False
        self.kill_calls = 0

    @property
    def is_dead(self) -> bool:
        return self.browser is None or self.cdp_closed

    def start(self):
        self.browser = object()

    def close(self):
        self.browser = None

    def kill(self):
        self.kill_calls += 1
        self.cdp_closed = True


class _Database:
    def get(self, scrape_id):
        return {"ID": scrape_id}


def _run(monkeypatch, browser, error, closes_connection):
    def fail(self, first_keyword):
        if closes_connection:
            browser.cdp_closed = True
        raise error

    monkeypatch.setattr(ScrapeRun, "_search", fail)
    settings = ScrapeSettings(keywords=("bakery",))
    scrape = cast(
        ScrapeRecord,
        {
            "ID": SCRAPE_ID,
            "Name": "bakery",
            "Date": "2026-11-15T00:00:00",
            "Status": "working",
            "Data": cast(SettingsData, settings.model_dump(by_alias=True, mode="json")),
            "Error": "",
            "Progress": {},
        },
    )
    run = ScrapeRun(
        cast(ScrapeDatabase, _Database()),
        scrape,
        True,
        fakeredis.FakeRedis(decode_responses=True),
        browser=cast(BrowserSession, browser),
    )
    run.execute()
    assert run.outcome is not None
    return run.outcome


def test_a_cdp_timeout_on_a_live_connection_is_not_a_lost_chrome(monkeypatch):
    browser = _StubBrowserSession()
    error = CDPError("Page.navigate: no reply within 30 s")
    outcome = _run(monkeypatch, browser, error, closes_connection=False)
    assert outcome.connection_lost is False
    assert outcome.note


def test_a_proxy_oserror_on_a_live_connection_is_not_a_lost_chrome(monkeypatch):
    browser = _StubBrowserSession()
    outcome = _run(
        monkeypatch,
        browser,
        OSError("proxy refused the tunnel"),
        closes_connection=False,
    )
    assert outcome.connection_lost is False


def test_a_cdp_error_that_closes_the_connection_is_a_lost_chrome(monkeypatch):
    browser = _StubBrowserSession()
    error = CDPError("Page.navigate: connection lost: gone")
    outcome = _run(monkeypatch, browser, error, closes_connection=True)
    assert outcome.connection_lost is True


def test_a_watchdog_kill_is_not_a_lost_chrome(monkeypatch):
    browser = _StubBrowserSession()

    def fail(self, first_keyword):
        self._killed_by_watchdog = True
        browser.cdp_closed = True
        raise CDPError("Page.navigate: connection lost: killed")

    monkeypatch.setattr(ScrapeRun, "_search", fail)
    settings = ScrapeSettings(keywords=("bakery",))
    scrape = cast(
        ScrapeRecord,
        {
            "ID": SCRAPE_ID,
            "Name": "bakery",
            "Date": "2026-11-15T00:00:00",
            "Status": "working",
            "Data": cast(SettingsData, settings.model_dump(by_alias=True, mode="json")),
            "Error": "",
            "Progress": {},
        },
    )
    run = ScrapeRun(
        cast(ScrapeDatabase, _Database()),
        scrape,
        True,
        fakeredis.FakeRedis(decode_responses=True),
        browser=cast(BrowserSession, browser),
    )
    run.execute()
    assert run.outcome is not None
    assert run.outcome.connection_lost is False
