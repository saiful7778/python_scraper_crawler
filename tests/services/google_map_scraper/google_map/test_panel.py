from typing import cast

import pytest

from app.services.browser import BrowserTab
from app.services.browser.cdp_error import CDPError
from app.services.google_map_scraper.google_map import panel
from tests.services.google_map_scraper.google_map.fakes import (
    FakeClock,
    FakeTab,
    Script,
)

URL = "https://www.google.com/maps/place/Cafe/data=!19sChIJabc"
PANEL_URL = "https://www.google.com/maps/preview/place?authuser=0"
JSON_MIME = "application/json; charset=utf-8"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(panel, "time", fake)
    return fake


def _response(request_id: str, url: str, mime: str = JSON_MIME) -> dict:
    return {"requestId": request_id, "response": {"url": url, "mimeType": mime}}


def test_returns_body_of_first_matching_json_response(clock: FakeClock):
    tab = FakeTab()
    tab.client.events.put(_response("R0", "https://www.google.com/other?x=1"))
    tab.client.events.put(_response("R1", PANEL_URL))
    tab.replies["Network.getResponseBody"] = Script({"body": "PANEL"})

    body = panel.fetch_lead_panel_json(cast(BrowserTab, tab), URL)

    assert body == "PANEL"
    assert tab.navigated == [URL]
    assert tab.calls == [("Network.getResponseBody", {"requestId": "R1"})]


def test_subscribes_to_response_events_and_unsubscribes_afterwards(
    clock: FakeClock,
):
    tab = FakeTab()
    tab.client.events.put(_response("R1", PANEL_URL))
    tab.replies["Network.getResponseBody"] = Script({"body": "PANEL"})

    panel.fetch_lead_panel_json(cast(BrowserTab, tab), URL)

    assert tab.client.subscriptions == [
        ("Network.responseReceived", "S1", tab.client.events)
    ]
    assert tab.client.unsubscribed == [
        ("Network.responseReceived", tab.client.events, "S1")
    ]


def test_ignores_matching_url_that_is_not_json(clock: FakeClock):
    tab = FakeTab()
    tab.client.events.put(_response("R1", PANEL_URL, mime="text/html"))

    body = panel.fetch_lead_panel_json(cast(BrowserTab, tab), URL, timeout=1.0)

    assert body == ""
    assert tab.calls == []


def test_returns_empty_on_timeout_without_reading_body(clock: FakeClock):
    tab = FakeTab()

    body = panel.fetch_lead_panel_json(cast(BrowserTab, tab), URL, timeout=1.0)

    assert body == ""
    assert clock.now >= 1.0
    assert tab.calls == []
    assert len(tab.client.unsubscribed) == 1


def test_returns_empty_when_google_serves_sorry_page(clock: FakeClock):
    tab = FakeTab()
    tab.redirect_to = "https://www.google.com/sorry/index?continue=x"
    tab.client.events.put(_response("R1", PANEL_URL))

    body = panel.fetch_lead_panel_json(cast(BrowserTab, tab), URL)

    assert body == ""
    assert tab.calls == []
    assert len(tab.client.unsubscribed) == 1


def test_retries_body_read_while_response_is_still_downloading(clock: FakeClock):
    tab = FakeTab()
    tab.client.events.put(_response("R1", PANEL_URL))
    tab.replies["Network.getResponseBody"] = Script(
        CDPError("No data found"), {"body": "READY"}
    )

    body = panel.fetch_lead_panel_json(cast(BrowserTab, tab), URL)

    assert body == "READY"
    assert len(tab.calls) == 2


def test_reraises_body_read_error_when_connection_is_closed(clock: FakeClock):
    tab = FakeTab()
    tab.client.closed = OSError("socket gone")
    tab.client.events.put(_response("R1", PANEL_URL))
    tab.replies["Network.getResponseBody"] = Script(CDPError("gone"))

    with pytest.raises(CDPError, match="gone"):
        panel.fetch_lead_panel_json(cast(BrowserTab, tab), URL)

    assert len(tab.client.unsubscribed) == 1


def test_raises_when_event_queue_delivers_connection_loss(clock: FakeClock):
    tab = FakeTab()
    tab.client.events.put(object())

    with pytest.raises(CDPError, match="connection lost"):
        panel.fetch_lead_panel_json(cast(BrowserTab, tab), URL)

    assert len(tab.client.unsubscribed) == 1
