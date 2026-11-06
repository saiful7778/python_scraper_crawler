import queue

import pytest

from app.services.browser import proxy
from app.services.browser.proxy import ProxyAuthenticator, split_proxy_url


class FakeClient:
    def __init__(self) -> None:
        self.closed = False
        self.subscriptions: list[tuple[str, str]] = []
        self.unsubscribed: list[str] = []

    def subscribe_events(self, method, session_id, event_queue=None):
        self.subscriptions.append((method, session_id))
        return event_queue if event_queue is not None else queue.Queue()

    def unsubscribe_events(self, method, event_queue, session_id):
        self.unsubscribed.append(method)


class FakeTab:
    def __init__(self) -> None:
        self.client = FakeClient()
        self.session_id = "S1"
        self.calls: list[tuple[str, dict]] = []

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        return {}

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


@pytest.fixture
def no_reader_thread(monkeypatch):
    """Keep the background reader from consuming events under test."""

    class IdleThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(proxy.threading, "Thread", IdleThread)


@pytest.fixture
def tab() -> FakeTab:
    return FakeTab()


@pytest.fixture
def auth(tab, no_reader_thread) -> ProxyAuthenticator:
    return ProxyAuthenticator(tab, "alice", "s3cret")


def _proxy_challenge(request_id: str) -> dict:
    return {
        "requestId": request_id,
        "authChallenge": {"source": "Proxy", "origin": "http://proxy"},
    }


def test_init_enables_fetch_with_auth_handling(auth, tab):
    assert tab.calls == [
        (
            "Fetch.enable",
            {"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]},
        )
    ]
    assert ("Fetch.requestPaused", "S1") in tab.client.subscriptions
    assert ("Fetch.authRequired", "S1") in tab.client.subscriptions


def test_ordinary_request_is_continued(auth, tab):
    auth._handle("r1", {"requestId": "r1"})

    assert tab.calls[-1] == ("Fetch.continueRequest", {"requestId": "r1"})


def test_first_proxy_challenge_gets_credentials(auth, tab):
    auth._handle("r1", _proxy_challenge("r1"))

    method, params = tab.calls[-1]
    assert method == "Fetch.continueWithAuth"
    assert params["requestId"] == "r1"
    assert params["authChallengeResponse"] == {
        "response": "ProvideCredentials",
        "username": "alice",
        "password": "s3cret",
    }
    assert auth.refused is False


def test_repeated_proxy_challenge_is_a_refusal(auth, tab):
    auth._handle("r1", _proxy_challenge("r1"))
    auth._handle("r1", _proxy_challenge("r1"))

    method, params = tab.calls[-1]
    assert method == "Fetch.continueWithAuth"
    assert params["authChallengeResponse"] == {"response": "CancelAuth"}
    assert auth.refused is True


def test_non_proxy_challenge_uses_default_handling(auth, tab):
    auth._handle(
        "r2",
        {"requestId": "r2", "authChallenge": {"source": "Server"}},
    )

    assert tab.calls[-1] == (
        "Fetch.continueWithAuth",
        {"requestId": "r2", "authChallengeResponse": {"response": "Default"}},
    )
    assert auth.refused is False


def test_run_processes_events_in_order_until_sentinel(auth, tab):
    events = [
        {"requestId": "a"},
        _proxy_challenge("b"),
        {"no": "requestId"},
        None,
    ]
    for event in events:
        auth.event_queue.put(event)

    auth._run()

    assert tab.methods()[1:] == [
        "Fetch.continueRequest",
        "Fetch.continueWithAuth",
    ]


def test_run_stops_when_closed(auth, tab):
    auth.close()
    auth.event_queue.put({"requestId": "a"})

    auth._run()

    assert tab.methods() == ["Fetch.enable"]


def test_close_unsubscribes_from_both_fetch_events(auth, tab):
    auth.close()

    assert auth._done.is_set()
    assert sorted(tab.client.unsubscribed) == [
        "Fetch.authRequired",
        "Fetch.requestPaused",
    ]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "http://user:pass@proxy.example:8080",
            ("http://proxy.example:8080", "user", "pass"),
        ),
        (
            "socks5h://proxy.example:1080",
            ("socks5://proxy.example:1080", "", ""),
        ),
        (
            "http://[2001:db8::1]:3128",
            ("http://[2001:db8::1]:3128", "", ""),
        ),
        (
            "https://proxy.example",
            ("https://proxy.example", "", ""),
        ),
    ],
)
def test_split_proxy_url(url, expected):
    assert split_proxy_url(url) == expected


def test_split_proxy_url_percent_decodes_credentials():
    _, username, password = split_proxy_url("http://us%40er:p%23ss@host:1")

    assert username == "us@er"
    assert password == "p#ss"
