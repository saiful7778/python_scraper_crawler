import io
import urllib.request

from app.utils import http_fetcher
from app.utils.http_fetcher import (
    SITE_HARD,
    SITE_TIMEOUT,
    UA,
    FetchDeadline,
    FetchWindow,
    PublicOnlyHTTPConnection,
    PublicOnlyRedirectHandler,
    WebPageFetcher,
    fetch_web_page,
    read_response_capped,
)
from app.utils.network_guard import create_public_connection


class _FakeOpener:
    def __init__(self, body: bytes = b"", error: Exception | None = None):
        self._body = body
        self._error = error
        self.requests: list[urllib.request.Request] = []

    def open(self, request, timeout):
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return io.BytesIO(self._body)


def _public(monkeypatch, public: bool = True) -> None:
    monkeypatch.setattr(
        http_fetcher, "resolves_only_to_public_addresses", lambda host: public
    )


def test_read_response_capped_reads_whole_body() -> None:
    assert read_response_capped(io.BytesIO(b"hello"), 100, 5) == b"hello"


def test_read_response_capped_stops_at_byte_limit() -> None:
    assert read_response_capped(io.BytesIO(b"x" * 10), 4, 5) == b"xxxx"


def test_read_response_capped_without_time_reads_nothing() -> None:
    assert read_response_capped(io.BytesIO(b"hello"), 100, 0) == b""


def test_read_response_capped_keeps_data_read_before_an_error() -> None:
    class Broken:
        def __init__(self):
            self._calls = 0

        def read1(self, size):
            self._calls += 1
            if self._calls == 1:
                return b"partial"
            raise OSError("connection reset")

    assert read_response_capped(Broken(), 100, 5) == b"partial"


def test_fetch_deadline_window_sets_and_clears_deadline() -> None:
    FetchDeadline.begin_window()
    assert FetchDeadline._local.deadline is not None
    FetchDeadline.end_window()
    assert FetchDeadline._local.deadline is None


def test_fetch_window_is_a_context_manager() -> None:
    with FetchWindow():
        assert FetchDeadline._local.deadline is not None
    assert FetchDeadline._local.deadline is None


def test_fetch_deadline_arm_and_cancel_timer() -> None:
    class _Sock:
        def shutdown(self, how):
            pass

    class _Connection:
        sock = _Sock()

    FetchDeadline.begin_window()
    try:
        FetchDeadline.arm(_Connection())
        assert FetchDeadline._local.timer is not None
        FetchDeadline.cancel_timer()
        assert FetchDeadline._local.timer is None
    finally:
        FetchDeadline.end_window()


def test_public_only_connections_use_public_connection_guard() -> None:
    assert PublicOnlyHTTPConnection("example.com")._create_connection is (
        create_public_connection
    )


def test_redirect_to_non_http_scheme_is_refused() -> None:
    request = urllib.request.Request("http://a.example/")
    handler = PublicOnlyRedirectHandler()
    assert (
        handler.redirect_request(request, None, 302, "Found", {}, "ftp://b.example/x")
        is None
    )


def test_redirect_to_non_public_host_is_refused(monkeypatch) -> None:
    _public(monkeypatch, public=False)
    request = urllib.request.Request("http://a.example/")
    handler = PublicOnlyRedirectHandler()
    assert (
        handler.redirect_request(request, None, 302, "Found", {}, "http://10.0.0.1/")
        is None
    )


def test_redirect_to_public_host_is_followed(monkeypatch) -> None:
    _public(monkeypatch, public=True)
    request = urllib.request.Request("http://a.example/")
    handler = PublicOnlyRedirectHandler()
    follow_up = handler.redirect_request(
        request, None, 302, "Found", {}, "http://b.example/next"
    )
    assert follow_up is not None
    assert follow_up.full_url == "http://b.example/next"


def test_fetch_returns_decoded_page(monkeypatch) -> None:
    _public(monkeypatch)
    fetcher = WebPageFetcher()
    fetcher._opener = _FakeOpener("<html>héllo</html>".encode())
    assert fetcher.fetch("https://shop.example/") == "<html>héllo</html>"


def test_fetch_adds_http_scheme_and_sends_user_agent(monkeypatch) -> None:
    _public(monkeypatch)
    fetcher = WebPageFetcher()
    opener = _FakeOpener(b"ok")
    fetcher._opener = opener
    fetcher.fetch("shop.example/about")
    request = opener.requests[0]
    assert request.full_url == "http://shop.example/about"
    assert request.get_header("User-agent") == UA


def test_fetch_encodes_non_ascii_url(monkeypatch) -> None:
    _public(monkeypatch)
    fetcher = WebPageFetcher()
    opener = _FakeOpener(b"ok")
    fetcher._opener = opener
    fetcher.fetch("https://shop.example/café")
    assert opener.requests[0].full_url == "https://shop.example/caf%C3%A9"


def test_fetch_replaces_invalid_utf8(monkeypatch) -> None:
    _public(monkeypatch)
    fetcher = WebPageFetcher()
    fetcher._opener = _FakeOpener(b"bad\xff")
    assert fetcher.fetch("https://shop.example/") == "bad\ufffd"


def test_fetch_non_public_host_is_empty_without_request(monkeypatch) -> None:
    _public(monkeypatch, public=False)
    fetcher = WebPageFetcher()
    opener = _FakeOpener(b"secret")
    fetcher._opener = opener
    assert fetcher.fetch("http://192.168.0.1/") == ""
    assert opener.requests == []


def test_fetch_error_is_empty_page(monkeypatch) -> None:
    _public(monkeypatch)
    fetcher = WebPageFetcher()
    fetcher._opener = _FakeOpener(error=OSError("timed out"))
    assert fetcher.fetch("https://shop.example/") == ""


def test_fetch_web_page_uses_default_fetcher(monkeypatch) -> None:
    _public(monkeypatch)
    opener = _FakeOpener(b"page")
    monkeypatch.setattr(http_fetcher._DEFAULT_FETCHER, "_opener", opener)
    assert fetch_web_page("https://shop.example/", timeout=SITE_TIMEOUT) == "page"


def test_module_limits_are_sane() -> None:
    assert 0 < SITE_TIMEOUT <= SITE_HARD
