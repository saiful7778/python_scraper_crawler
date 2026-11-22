import json

import httpx
import pytest

from app.services.serper.maps.serper_map_client import SerperMapClient
from app.services.serper.maps.serper_map_errors import (
    SerperAuthError,
    SerperNoCreditsError,
    SerperRateLimitedError,
    SerperResponseError,
    SerperUnavailableError,
)
from app.services.serper.maps.serper_map_type import Place, SearchResult


def _transport(status: int, body: dict, headers: dict | None = None):
    return httpx.MockTransport(
        lambda request: httpx.Response(status, json=body, headers=headers or {})
    )


async def test_search_sends_the_keyword_ll_and_page():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["X-API-KEY"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"places": [], "credits": 3, "ll": "@30.1,-97.7,13z"}
        )

    client = SerperMapClient("k1", transport=httpx.MockTransport(handler))
    await client.search("plumbers", 30.1, -97.7, 13, 2)
    assert seen["url"] == "https://google.serper.dev/maps"
    assert seen["key"] == "k1"
    assert seen["body"] == {"q": "plumbers", "ll": "@30.1,-97.7,13z", "page": 2}
    await client.aclose()


async def test_search_returns_typed_places():
    body = {
        "places": [
            {
                "position": 1,
                "title": "Cafe",
                "latitude": 30.1,
                "longitude": -97.7,
                "rating": 4.5,
                "ratingCount": 12,
                "type": "Cafe",
                "types": ["Cafe"],
                "cid": "9",
                "fid": "0x1:0x2",
                "placeId": "p1",
                "phoneNumber": "(512) 555-0100",
                "website": "https://a.com",
                "openingHours": {"Monday": "9 AM-5 PM"},
            }
        ],
        "credits": 3,
        "ll": "@30.1,-97.7,13z",
    }
    client = SerperMapClient("k1", transport=_transport(200, body))
    result = await client.search("plumbers", 30.1, -97.7, 13, 1)
    place = result.places[0]
    assert isinstance(place, Place)
    assert place.placeId == "p1" and place.phoneNumber == "(512) 555-0100"
    assert place.openingHours is not None
    assert place.openingHours.Monday == "9 AM-5 PM"
    assert place.openingHours.Tuesday is None
    assert result.credits == 3
    await client.aclose()


async def test_a_place_missing_optional_fields_still_builds():
    body = {"places": [{"position": 1, "title": "Bare"}], "credits": 3}
    client = SerperMapClient("k1", transport=_transport(200, body))
    place = (await client.search("x", 30.0, -97.0, 13, 1)).places[0]
    assert place.website is None and place.types == [] and place.openingHours is None
    await client.aclose()


@pytest.mark.parametrize(
    "status,message,error",
    [
        (402, "Payment required", SerperNoCreditsError),
        (403, "Unauthorized.", SerperAuthError),
        (403, "Your plan has no credits left", SerperNoCreditsError),
        (403, "quota exceeded", SerperNoCreditsError),
        (401, "Unauthorized", SerperAuthError),
        (400, "Bad query", SerperResponseError),
        (500, "Server error", SerperUnavailableError),
        (503, "Unavailable", SerperUnavailableError),
    ],
)
async def test_http_failures_raise_typed_errors(status, message, error):
    client = SerperMapClient("k1", transport=_transport(status, {"message": message}))
    with pytest.raises(error) as info:
        await client.search("x", 30.0, -97.0, 13, 1)
    assert str(status) in str(info.value) or message in str(info.value)
    await client.aclose()


async def test_429_carries_the_retry_after():
    client = SerperMapClient(
        "k1", transport=_transport(429, {"message": "slow down"}, {"Retry-After": "7"})
    )
    with pytest.raises(SerperRateLimitedError) as info:
        await client.search("x", 30.0, -97.0, 13, 1)
    assert info.value.retry_after == 7.0
    await client.aclose()


async def test_a_transport_failure_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = SerperMapClient("k1", transport=httpx.MockTransport(handler))
    with pytest.raises(SerperUnavailableError):
        await client.search("x", 30.0, -97.0, 13, 1)
    await client.aclose()


async def test_balance_reads_the_account_balance():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"balance": 1554, "rateLimit": 5})

    client = SerperMapClient("k1", transport=httpx.MockTransport(handler))
    assert await client.balance() == 1554
    assert seen["url"] == "https://google.serper.dev/account"
    await client.aclose()


async def test_the_key_never_appears_in_an_error_message():
    client = SerperMapClient(
        "secret-key-value", transport=_transport(400, {"message": "Bad query"})
    )
    with pytest.raises(SerperResponseError) as info:
        await client.search("x", 30.0, -97.0, 13, 1)
    assert "secret-key-value" not in str(info.value)
    await client.aclose()


async def test_no_place_is_ever_a_raw_dict():
    body = {"places": [{"position": 2, "title": "X"}], "credits": 3}
    client = SerperMapClient("k1", transport=_transport(200, body))
    result = await client.search("x", 30.0, -97.0, 13, 1)
    assert all(isinstance(place, Place) for place in result.places)
    assert isinstance(result, SearchResult)
    await client.aclose()


async def test_a_blank_page_is_a_normal_empty_result():
    client = SerperMapClient("k1", transport=_transport(200, {"places": [], "credits": 3}))
    result = await client.search("x", 30.0, -97.0, 13, 9)
    assert result.places == [] and result.credits == 3
    await client.aclose()
