import httpx
import pytest

from app.services.google_map_scraper.scrape import website_resolver
from app.services.google_map_scraper.scrape.website_resolver import resolve_website


@pytest.fixture(autouse=True)
def public_hosts(monkeypatch):
    monkeypatch.setattr(
        website_resolver, "resolves_only_to_public_addresses", lambda host: True
    )


def _transport(answers: dict[str, int], seen: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        if url in answers:
            return httpx.Response(answers[url], request=request)
        raise httpx.ConnectError("refused", request=request)

    return httpx.MockTransport(handler)


def test_apex_https_is_chosen_when_the_www_host_is_unreachable() -> None:
    answers = {"https://medixsignatureclinic.com/": 200}
    assert (
        resolve_website(
            "http://medixsignatureclinic.com/", transport=_transport(answers)
        )
        == "https://medixsignatureclinic.com/"
    )


def test_redirect_target_is_returned_as_the_final_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.com" and request.url.scheme == "https":
            return httpx.Response(
                301, headers={"location": "https://b.com/home"}, request=request
            )
        if str(request.url) == "https://b.com/home":
            return httpx.Response(200, request=request)
        raise httpx.ConnectError("refused", request=request)

    assert (
        resolve_website("a.com", transport=httpx.MockTransport(handler))
        == "https://b.com/home"
    )


def test_error_page_is_kept_when_no_candidate_works() -> None:
    answers = {"https://a.com/": 404, "http://a.com/": 404}
    assert resolve_website("a.com", transport=_transport(answers)) == "https://a.com/"


def test_raw_value_is_kept_when_nothing_answers() -> None:
    assert resolve_website("a.com", transport=_transport({})) == "a.com"


def test_non_public_host_is_never_used(monkeypatch) -> None:
    monkeypatch.setattr(
        website_resolver, "resolves_only_to_public_addresses", lambda host: False
    )
    seen: list[str] = []
    answers = {"https://a.com/": 200}
    assert (
        resolve_website("a.com", transport=_transport(answers, seen)) == "a.com"
    )
    assert seen == []


def test_empty_and_unusable_values_are_returned_unchanged() -> None:
    assert resolve_website("") == ""
    assert resolve_website("ftp://a.com") == "ftp://a.com"


def test_head_refused_falls_back_to_get() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(405, request=request)
        return httpx.Response(200, request=request)

    assert (
        resolve_website("https://a.com", transport=httpx.MockTransport(handler))
        == "https://a.com/"
    )
