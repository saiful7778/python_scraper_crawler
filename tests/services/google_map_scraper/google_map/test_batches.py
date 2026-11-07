from typing import Any, cast

import pytest

from app.services.browser.cdp_error import CDPError
from app.services.google_map_scraper.google_map import BatchResponseReader
from app.services.google_map_scraper.google_map.batches import _MAX_BODY_READ_TRIES
from tests.services.google_map_scraper.google_map.fakes import (
    FakeTab,
    Script,
)

BATCH_URL = "https://www.google.com/search?tbm=map&q=cafe"
OTHER_URL = "https://www.google.com/maps/preview/place?authuser=0"
JSON_MIME = "application/json; charset=utf-8"


@pytest.fixture
def tab() -> FakeTab:
    return FakeTab()


def _reader(tab: FakeTab) -> BatchResponseReader:
    return BatchResponseReader(cast(Any, tab))


@pytest.fixture(autouse=True)
def fake_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.google_map_scraper.google_map.batches.parse_place_list",
        lambda body: [{"body": body}],
    )


def _requested(request_id: str, url: str = BATCH_URL) -> dict:
    return {"requestId": request_id, "request": {"url": url}}


def _answered(request_id: str, url: str = BATCH_URL, **response) -> dict:
    response.setdefault("status", 200)
    response.setdefault("mimeType", JSON_MIME)
    return {"requestId": request_id, "response": {"url": url, **response}}


def _failed(request_id: str) -> dict:
    return {"requestId": request_id, "errorText": "net::ERR_ABORTED"}


def _push(tab: FakeTab, *messages: object) -> None:
    for message in messages:
        tab.client.events.put(message)


def test_subscribes_to_three_network_events_on_one_queue(tab: FakeTab):
    reader = _reader(tab)

    assert [(method, session) for method, session, _ in tab.client.subscriptions] == [
        ("Network.requestWillBeSent", "S1"),
        ("Network.responseReceived", "S1"),
        ("Network.loadingFailed", "S1"),
    ]
    assert all(q is reader.event_queue for _, _, q in tab.client.subscriptions)


def test_close_unsubscribes_from_all_three_events(tab: FakeTab):
    reader = _reader(tab)

    reader.close()

    assert [method for method, _, _ in tab.client.unsubscribed] == [
        "Network.requestWillBeSent",
        "Network.responseReceived",
        "Network.loadingFailed",
    ]
    assert all(q is reader.event_queue for _, q, _ in tab.client.unsubscribed)


def test_reads_and_parses_a_batch_once_its_body_is_available(tab: FakeTab):
    reader = _reader(tab)
    _push(tab, _requested("R1"), _answered("R1"))
    tab.replies["Network.getResponseBody"] = Script({"body": "JSON-1"})

    rows = reader.poll()

    assert rows == [{"body": "JSON-1"}]
    assert tab.calls == [("Network.getResponseBody", {"requestId": "R1"})]
    assert reader.count == 1
    assert reader.asked == 1
    assert reader.inflight == 0
    assert reader.pending == 0


def test_request_without_answer_is_inflight(tab: FakeTab):
    reader = _reader(tab)
    _push(tab, _requested("R1"))

    rows = reader.poll()

    assert rows == []
    assert reader.inflight == 1
    assert reader.asked == 1
    assert tab.calls == []


def test_ignores_requests_that_are_not_batch_urls(tab: FakeTab):
    reader = _reader(tab)
    _push(tab, _requested("R1", OTHER_URL), _answered("R1", OTHER_URL))

    reader.poll()

    assert reader.asked == 0
    assert reader.inflight == 0
    assert reader.pending == 0
    assert tab.calls == []


def test_counts_rate_limited_batch_without_reading_it(tab: FakeTab):
    reader = _reader(tab)
    _push(tab, _requested("R1"), _answered("R1", status=429))

    rows = reader.poll()

    assert rows == []
    assert reader.limited == 1
    assert reader.inflight == 0
    assert reader.pending == 0
    assert tab.calls == []


def test_non_json_batch_response_is_not_read(tab: FakeTab):
    reader = _reader(tab)
    _push(tab, _requested("R1"), _answered("R1", mimeType="text/html"))

    rows = reader.poll()

    assert rows == []
    assert reader.inflight == 0
    assert reader.pending == 0
    assert tab.calls == []


def test_failed_load_is_no_longer_inflight(tab: FakeTab):
    reader = _reader(tab)
    _push(tab, _requested("R1"), _failed("R1"))

    reader.poll()

    assert reader.asked == 1
    assert reader.inflight == 0
    assert tab.calls == []


def test_retries_body_read_until_it_succeeds(tab: FakeTab):
    reader = _reader(tab)
    _push(tab, _requested("R1"), _answered("R1"))
    tab.replies["Network.getResponseBody"] = Script(
        CDPError("No data found"), {"body": "late"}
    )

    first = reader.poll()
    assert first == []
    assert reader.pending == 1
    assert reader.count == 0

    second = reader.poll()
    assert second == [{"body": "late"}]
    assert reader.pending == 0
    assert reader.count == 1


def test_drops_batch_after_too_many_unreadable_bodies(tab: FakeTab):
    reader = _reader(tab)
    _push(tab, _requested("R1"), _answered("R1"))
    tab.replies["Network.getResponseBody"] = Script(CDPError("No data found"))

    for _ in range(_MAX_BODY_READ_TRIES):
        reader.poll()
    assert reader.pending == 1

    reader.poll()

    assert reader.pending == 0
    assert reader.count == 0


def test_reraises_body_read_error_when_connection_is_closed(tab: FakeTab):
    reader = _reader(tab)
    tab.client.closed = OSError("socket gone")
    _push(tab, _requested("R1"), _answered("R1"))
    tab.replies["Network.getResponseBody"] = Script(CDPError("gone"))

    with pytest.raises(CDPError, match="gone"):
        reader.poll()


def test_poll_raises_when_connection_is_lost(tab: FakeTab):
    reader = _reader(tab)
    tab.client.events.put(object())

    with pytest.raises(CDPError, match="connection lost"):
        reader.poll()


def test_reads_batches_in_the_order_they_were_answered(tab: FakeTab):
    reader = _reader(tab)
    _push(
        tab,
        _requested("R1"),
        _requested("R2"),
        _answered("R2"),
        _answered("R1"),
    )
    tab.replies["Network.getResponseBody"] = Script(
        {"body": "answered-first"}, {"body": "answered-second"}
    )

    rows = reader.poll()

    assert rows == [{"body": "answered-first"}, {"body": "answered-second"}]
    assert [params for _, params in tab.calls] == [
        {"requestId": "R2"},
        {"requestId": "R1"},
    ]
    assert reader.count == 2
    assert reader.asked == 2
    assert reader.inflight == 0
