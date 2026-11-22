"""One Serper /maps call, with its failures turned into the typed errors above."""

from typing import Protocol

import httpx

from app.utils.logger import get_logger

from .serper_map_errors import (
    SerperAuthError,
    SerperNoCreditsError,
    SerperRateLimitedError,
    SerperResponseError,
    SerperUnavailableError,
)
from .serper_map_type import SearchResult

logger = get_logger(__name__)

MAPS_URL = "https://google.serper.dev/maps"
ACCOUNT_URL = "https://google.serper.dev/account"

#: What Serper says when a key has spent its balance.
_CREDIT_HINTS = ("credit", "quota", "plan", "balance", "limit")
#: What Serper says when the key itself is not accepted.
_AUTH_HINTS = ("unauthorized", "invalid", "api key")

#: A page is 3 credits; a key with less left cannot serve one.
CREDITS_PER_PAGE = 3


def _message_of(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body)[:200]
    return str(body)[:200]


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class SerperMapCaller(Protocol):
    """What the scheduler needs from a key's caller, so a fake or another client fits."""

    async def search(
        self, keyword: str, lat: float, lon: float, zoom: int, page: int
    ) -> SearchResult: ...

    async def balance(self) -> int: ...

    async def aclose(self) -> None: ...


class SerperMapClient:
    """Calls the Serper maps endpoint with one API key.

    The key is never part of an error message or a log line. Retries and pacing
    are the caller's business: this class makes exactly one request per call.
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
            },
        )

    async def search(
        self, keyword: str, lat: float, lon: float, zoom: int, page: int
    ) -> SearchResult:
        """Search one page of ``keyword`` around a point."""
        payload = {"q": keyword, "ll": f"@{lat},{lon},{zoom}z", "page": page}
        response = await self._post(MAPS_URL, payload)
        return SearchResult.from_api(response)

    async def balance(self) -> int:
        """The credits the key has left, as Serper's account endpoint reports them."""
        response = await self._get(ACCOUNT_URL)
        balance = response.get("balance")
        try:
            return int(balance) if balance is not None else 0
        except (TypeError, ValueError):
            return 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, url: str, payload: dict) -> dict:
        try:
            response = await self._client.post(url, json=payload)
        except httpx.TransportError as error:
            raise SerperUnavailableError(f"the request failed: {error!r}") from error
        return self._decode(response)

    async def _get(self, url: str) -> dict:
        try:
            response = await self._client.get(url)
        except httpx.TransportError as error:
            raise SerperUnavailableError(f"the request failed: {error!r}") from error
        return self._decode(response)

    @staticmethod
    def _decode(response: httpx.Response) -> dict:
        """Return the body of a good answer, or raise the error its status means."""
        status = response.status_code
        if 200 <= status < 300:
            try:
                body = response.json()
            except ValueError as error:
                raise SerperUnavailableError(
                    f"the answer is not JSON: {error!r}"
                ) from error
            return body if isinstance(body, dict) else {}
        message = _message_of(response)
        lowered = message.lower()
        if status == 429:
            raise SerperRateLimitedError(
                f"HTTP 429: {message}", retry_after=_retry_after(response)
            )
        if status == 402:
            raise SerperNoCreditsError(f"HTTP 402: {message}")
        if status == 403:
            if any(hint in lowered for hint in _CREDIT_HINTS) and not any(
                hint in lowered for hint in _AUTH_HINTS
            ):
                raise SerperNoCreditsError(f"HTTP 403: {message}")
            raise SerperAuthError(f"HTTP 403: {message}")
        if status == 401:
            raise SerperAuthError(f"HTTP 401: {message}")
        if status >= 500:
            raise SerperUnavailableError(f"HTTP {status}: {message}")
        raise SerperResponseError(status, message)
