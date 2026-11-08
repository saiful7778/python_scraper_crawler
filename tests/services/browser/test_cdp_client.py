import logging
import threading
from typing import Any

import pytest

from app.services.browser.cdp_client import CDPClient
from app.services.browser.cdp_error import CDPError
from app.services.browser.ws_client import WebSocketClosed
from tests.services.browser.fakes import FakeWebSocket, wait_until


def test_call_returns_the_result_of_the_matching_reply(
    client: CDPClient, fake_ws: FakeWebSocket
):
    fake_ws.responder = lambda request: [
        {"id": request["id"], "result": {"frameId": "F1"}}
    ]

    assert client.call("Page.getFrameTree") == {"frameId": "F1"}


def test_call_sends_method_params_and_session_id(
    client: CDPClient, fake_ws: FakeWebSocket
):
    fake_ws.responder = lambda request: [{"id": request["id"], "result": {}}]

    client.call("Page.navigate", {"url": "https://example.com"}, session_id="S1")

    assert fake_ws.sent == [
        {
            "id": 1,
            "method": "Page.navigate",
            "params": {"url": "https://example.com"},
            "sessionId": "S1",
        }
    ]


def test_call_omits_session_id_and_defaults_params_to_empty(
    client: CDPClient, fake_ws: FakeWebSocket
):
    fake_ws.responder = lambda request: [{"id": request["id"], "result": {}}]

    client.call("Browser.getVersion")

    assert fake_ws.sent == [{"id": 1, "method": "Browser.getVersion", "params": {}}]


def test_call_returns_empty_dict_when_reply_has_no_result(
    client: CDPClient, fake_ws: FakeWebSocket
):
    fake_ws.responder = lambda request: [{"id": request["id"]}]

    assert client.call("Page.enable") == {}


def test_call_raises_cdp_error_with_method_and_message(
    client: CDPClient, fake_ws: FakeWebSocket
):
    fake_ws.responder = lambda request: [
        {"id": request["id"], "error": {"message": "'Foo.bar' wasn't found"}}
    ]

    with pytest.raises(CDPError, match="Foo.bar: 'Foo.bar' wasn't found"):
        client.call("Foo.bar")


def test_call_times_out_when_no_reply_arrives(client: CDPClient):
    with pytest.raises(CDPError, match="Runtime.evaluate: no reply within 0.05 s"):
        client.call("Runtime.evaluate", timeout=0.05)


def test_concurrent_calls_are_matched_to_their_own_replies(
    client: CDPClient, fake_ws: FakeWebSocket
):
    requests: list[dict[str, Any]] = []

    def responder(request: dict[str, Any]) -> list[dict[str, Any]]:
        requests.append(request)
        if len(requests) < 2:
            return []
        # Reply in reverse order to prove routing is by id, not arrival order.
        return [
            {"id": r["id"], "result": {"echo": r["params"]["n"]}}
            for r in reversed(requests)
        ]

    fake_ws.responder = responder
    results: dict[int, Any] = {}

    def worker(n: int) -> None:
        results[n] = client.call("Test.echo", {"n": n}, timeout=2)

    threads = [threading.Thread(target=worker, args=(n,)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert results == {1: {"echo": 1}, 2: {"echo": 2}}


def test_send_failure_raises_cdp_error(client: CDPClient, fake_ws: FakeWebSocket):
    fake_ws.send_error = OSError("broken pipe")

    with pytest.raises(
        CDPError, match="Page.enable: connection lost while sending: OSError: broken pipe"
    ):
        client.call("Page.enable")


def test_call_after_reader_died_fails_immediately(
    client: CDPClient, fake_ws: FakeWebSocket
):
    fake_ws.push(OSError("socket reset"))
    wait_until(lambda: client.closed is not None)

    with pytest.raises(CDPError, match="connection lost: OSError: socket reset"):
        client.call("Page.enable", timeout=5)


def test_server_close_frame_is_not_logged_as_an_error(
    client: CDPClient, fake_ws: FakeWebSocket, caplog: pytest.LogCaptureFixture
):
    fake_ws.push(WebSocketClosed("websocket closed by the server"))
    wait_until(lambda: client.closed is not None)

    assert client.closed is not None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_pending_call_is_woken_when_connection_is_lost(
    client: CDPClient, fake_ws: FakeWebSocket
):
    sent = threading.Event()

    def responder(_: dict[str, Any]) -> list[Any]:
        sent.set()
        return []

    fake_ws.responder = responder
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            client.call("Runtime.evaluate", timeout=5)
        except CDPError as error:
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    assert sent.wait(2)
    fake_ws.push(OSError("connection reset"))
    thread.join(5)

    assert len(errors) == 1
    assert "connection lost: OSError: connection reset" in str(errors[0])


def test_wait_for_event_returns_event_params(client: CDPClient, fake_ws: FakeWebSocket):
    event_queue = client.subscribe_events("Page.loadEventFired")
    fake_ws.push({"method": "Page.loadEventFired", "params": {"timestamp": 1.5}})

    assert client.wait_for_event(
        "Page.loadEventFired", timeout=2, event_queue=event_queue
    ) == {"timestamp": 1.5}


def test_wait_for_event_only_receives_events_for_its_session(
    client: CDPClient, fake_ws: FakeWebSocket
):
    event_queue = client.subscribe_events("Page.loadEventFired", session_id="S1")
    fake_ws.push(
        {"method": "Page.loadEventFired", "sessionId": "S2", "params": {"from": "S2"}}
    )
    fake_ws.push(
        {"method": "Page.loadEventFired", "sessionId": "S1", "params": {"from": "S1"}}
    )

    assert client.wait_for_event(
        "Page.loadEventFired", "S1", timeout=2, event_queue=event_queue
    ) == {"from": "S1"}


def test_global_subscription_receives_events_without_session(
    client: CDPClient, fake_ws: FakeWebSocket
):
    event_queue = client.subscribe_events("Target.targetCreated")
    fake_ws.push({"method": "Target.targetCreated", "params": {"id": "T9"}})

    assert client.wait_for_event(
        "Target.targetCreated", timeout=2, event_queue=event_queue
    ) == {"id": "T9"}


def test_wait_for_event_times_out(client: CDPClient):
    with pytest.raises(CDPError, match="no Page.loadEventFired event within 0.05 s"):
        client.wait_for_event("Page.loadEventFired", timeout=0.05)


def test_unsubscribed_queue_stops_receiving_events(
    client: CDPClient, fake_ws: FakeWebSocket
):
    event_queue = client.subscribe_events("Page.frameNavigated")
    client.unsubscribe_events("Page.frameNavigated", event_queue)
    fake_ws.push({"method": "Page.frameNavigated", "params": {}})
    fake_ws.responder = lambda request: [{"id": request["id"], "result": {}}]

    # The reader handles messages in order, so once this reply returns the
    # earlier event has already been routed (or dropped).
    client.call("Test.sync")

    assert event_queue.empty()


def test_waiter_is_released_with_error_when_connection_is_lost(
    client: CDPClient, fake_ws: FakeWebSocket
):
    event_queue = client.subscribe_events("Page.loadEventFired")
    fake_ws.push(OSError("socket reset"))

    with pytest.raises(CDPError, match="connection lost: OSError: socket reset"):
        client.wait_for_event("Page.loadEventFired", timeout=5, event_queue=event_queue)


def test_close_closes_the_websocket(client: CDPClient, fake_ws: FakeWebSocket):
    client.close()

    assert fake_ws.closed
