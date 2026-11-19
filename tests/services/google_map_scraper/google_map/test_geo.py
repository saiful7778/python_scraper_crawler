from typing import Self

import pytest

from app.services.google_map_scraper.google_map import geo


def answer(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    class Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        def read(self) -> bytes:
            return body

    monkeypatch.setattr(geo.urllib.request, "urlopen", lambda *a, **k: Response())
    monkeypatch.setattr(geo.time, "sleep", lambda seconds: None)


class TestGetGeocodeHit:
    def test_returns_the_first_hit_with_its_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answer(
            monkeypatch,
            b'[{"lat":"-27.46","lon":"153.02","type":"suburb",'
            b'"display_name":"Spring Hill, Queensland",'
            b'"boundingbox":["-27.47","-27.45","153.01","153.03"]}]',
        )

        assert geo.get_geocode_hit("Spring Hill", country="au") == {
            "lat": -27.46,
            "lon": 153.02,
            "type": "suburb",
            "display_name": "Spring Hill, Queensland",
            "boundingbox": ["-27.47", "-27.45", "153.01", "153.03"],
        }

    def test_no_match_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answer(monkeypatch, b"[]")
        assert geo.get_geocode_hit("nowhere") is None

    def test_missing_coordinates_are_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answer(monkeypatch, b'[{"name": "x"}]')
        with pytest.raises(geo.GeocodingError, match="no usable lat/lon"):
            geo.get_geocode_hit("dhaka")

    def test_missing_optional_metadata_becomes_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answer(monkeypatch, b'[{"lat":"1","lon":"2"}]')
        hit = geo.get_geocode_hit("somewhere")
        assert hit is not None
        assert hit["type"] == ""
        assert hit["display_name"] == ""
        assert hit["boundingbox"] is None
