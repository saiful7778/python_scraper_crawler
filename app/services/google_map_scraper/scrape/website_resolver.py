"""Find the address that works for a listing's website, while the scrape runs."""

import urllib.parse

import httpx

from app.utils.logger import get_logger
from app.utils.network_guard import resolves_only_to_public_addresses
from app.utils.url_utils import website_candidates

logger = get_logger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 8.0
HEAD_REFUSED = frozenset({400, 403, 405, 501})


def resolve_website(
    raw: str,
    timeout: float = TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Return the working address of ``raw``, or ``raw`` unchanged when nothing answers.

    Candidates are tried in the order of :func:`website_candidates`. A candidate
    counts when it answers with a status below 400 and its host, after any
    redirects, resolves only to public addresses. An error page that still answers
    is kept when no candidate works, so the site is still known to exist.
    """
    if not raw.strip():
        return raw
    try:
        candidates = website_candidates(raw)
    except ValueError as error:
        logger.debug("website %r is not a usable address: %s", raw, error)
        return raw
    public: dict[str, bool] = {}
    fallback = None
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        transport=transport,
    ) as client:
        for url in candidates:
            if not _is_public(url, public):
                continue
            try:
                response = _probe(client, url)
            except httpx.HTTPError as error:
                logger.debug("website %s did not answer: %s", url, error)
                continue
            final = str(response.url)
            if not _is_public(final, public):
                continue
            if response.status_code < 400:
                return final
            fallback = fallback or final
    return fallback or raw


def _probe(client: httpx.Client, url: str) -> httpx.Response:
    try:
        response = client.head(url)
        if response.status_code not in HEAD_REFUSED:
            return response
    except httpx.TransportError:
        pass
    return client.get(url)


def _is_public(url: str, cache: dict[str, bool]) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host not in cache:
        cache[host] = resolves_only_to_public_addresses(host)
    return cache[host]
