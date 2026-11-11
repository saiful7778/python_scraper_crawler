import logging
from typing import Self

import pytest

from app.services.browser.browser_error import BrowserError
from app.services.browser.cdp_error import CDPError
from app.services.browser.proxy import split_proxy_url
from app.services.browser.website.render_session import (
    UnsupportedURLError,
    _parse_web_url,
)
from app.services.google_map_scraper.extract_details import parse_place_list
from app.services.google_map_scraper.google_map import geo
from app.services.google_map_scraper.scraper import BoundingBoxError, parse_bounding_box
from app.utils.errors import ConfigError, ProxyConfigError, ScraperError, describe
from app.utils.proxies import parse_proxy_urls


class TestDescribe:
    def test_includes_type_and_message(self) -> None:
        assert describe(ValueError("bad port")) == "ValueError: bad port"

    def test_empty_message_still_names_the_type(self) -> None:
        assert describe(ConnectionResetError()) == "ConnectionResetError"


class TestErrorHierarchy:
    def test_project_errors_share_the_base(self) -> None:
        assert issubclass(BrowserError, ScraperError)
        assert issubclass(CDPError, ScraperError)
        assert issubclass(ProxyConfigError, ScraperError)
        assert issubclass(ConfigError, ScraperError)

    def test_existing_catches_still_match(self) -> None:
        assert issubclass(BrowserError, RuntimeError)
        assert issubclass(ProxyConfigError, ValueError)
        assert issubclass(UnsupportedURLError, ValueError)


class TestProxyErrors:
    def test_bad_entry_is_named_by_position_without_its_password(self) -> None:
        with pytest.raises(ProxyConfigError) as info:
            parse_proxy_urls(
                ["http://user:secret@proxy.example:8080", "http://:9999"]
            )
        message = str(info.value)
        assert "invalid proxy 2" in message
        assert "secret" not in message

    def test_malformed_proxy_url_for_chrome_does_not_echo_it(self) -> None:
        with pytest.raises(ProxyConfigError) as info:
            split_proxy_url("http://user:secret@[::1/")
        assert "secret" not in str(info.value)


class TestRenderUrlErrors:
    def test_credentials_are_refused_and_not_echoed(self) -> None:
        with pytest.raises(UnsupportedURLError, match="credentials") as info:
            _parse_web_url("https://user:secret@example.com")
        assert "secret" not in str(info.value)

    def test_unsupported_scheme_is_named(self) -> None:
        with pytest.raises(UnsupportedURLError, match="ftp"):
            _parse_web_url("ftp://example.com")


class TestGeocodingErrors:
    @staticmethod
    def _answer(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
        class Response:
            def __enter__(self) -> Self:
                return self

            def __exit__(self, *exc_info: object) -> None:
                return None

            def read(self) -> bytes:
                return body

        monkeypatch.setattr(geo.urllib.request, "urlopen", lambda *a, **k: Response())
        monkeypatch.setattr(geo.time, "sleep", lambda seconds: None)

    def test_no_match_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._answer(monkeypatch, b"[]")
        assert geo.get_geocode("nowhere") is None

    def test_not_json_names_the_place(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._answer(monkeypatch, b"<html>blocked</html>")
        with pytest.raises(geo.GeocodingError, match="'dhaka' is not JSON"):
            geo.get_geocode("dhaka")

    def test_object_instead_of_list_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._answer(monkeypatch, b'{"error": "rate limited"}')
        with pytest.raises(geo.GeocodingError, match="not a list"):
            geo.get_geocode("dhaka")

    def test_missing_coordinates_are_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._answer(monkeypatch, b'[{"name": "x"}]')
        with pytest.raises(geo.GeocodingError, match="no usable lat/lon"):
            geo.get_geocode("dhaka")


class TestBoundingBox:
    def test_four_numbers_parse(self) -> None:
        assert parse_bounding_box("dhaka", ["1", "2", "3", "4"]) == (1.0, 2.0, 3.0, 4.0)

    def test_wrong_count_is_named(self) -> None:
        with pytest.raises(BoundingBoxError, match="has 2 values, expected 4"):
            parse_bounding_box("dhaka", ["1", "2"])

    def test_non_number_is_named(self) -> None:
        with pytest.raises(BoundingBoxError, match="not a number"):
            parse_bounding_box("dhaka", ["1", "x", "3", "4"])


class TestMalformedSearchBatch:
    def test_invalid_json_is_logged_and_gives_no_rows(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            assert parse_place_list("not json") == []
        assert "search batch is not JSON" in caplog.text
