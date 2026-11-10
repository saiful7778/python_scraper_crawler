"""The real network transport, built on urllib."""

import http.client
import threading
import urllib.error
import urllib.request
from contextlib import closing

from app.utils.errors import describe
from app.utils.url_utils import encode_url_as_ascii

from ..body import should_read_body
from ..constants import READ_CHUNK_BYTES
from ..errors import (
    FetchError,
    TransportError,
    classify_network_error,
)
from ..hard_stop import FetchDeadline
from ..models import BodyRead, FetchPolicy, RawResponse
from ..responses import normalize_headers
from ..timing import (
    SYSTEM_CLOCK,
    Clock,
    Deadline,
    raise_if_cancelled,
)
from ..transport.connections import (
    NoRedirectHandler,
    PublicOnlyHTTPHandler,
    PublicOnlyHTTPSHandler,
)


def build_request_headers(policy: FetchPolicy) -> dict[str, str]:
    return {"User-Agent": policy.user_agent, "Accept": policy.accept}


def read_response_capped(
    response,
    max_bytes: int,
    max_seconds: float,
    *,
    cancel_event: threading.Event | None = None,
    deadline: Deadline | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> BodyRead:
    """Read up to ``max_bytes`` from ``response`` within ``max_seconds``.

    A cut-off or torn-down connection ends the read with what arrived and says
    why in :attr:`BodyRead.error`.
    """
    received = bytearray()
    end = clock.monotonic() + max_seconds
    error: FetchError | None = None
    while len(received) < max_bytes:
        raise_if_cancelled(cancel_event)
        if clock.monotonic() >= end or (deadline is not None and deadline.expired):
            error = FetchError.TIMEOUT
            break
        try:
            chunk = response.read1(min(READ_CHUNK_BYTES, max_bytes - len(received)))
        except (OSError, ValueError, http.client.HTTPException) as exc:
            expired = deadline is not None and deadline.expired
            error = classify_network_error(exc, expired)
            break
        if not chunk:
            # The hard stop shuts the socket down, which reads as a clean end.
            if deadline is not None and deadline.expired:
                error = FetchError.TIMEOUT
            break
        received += chunk
    return BodyRead(bytes(received), error)


class UrllibTransport:
    """:class:`Transport` on top of urllib, ignoring environment proxies."""

    def __init__(self, ssl_context=None):
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            PublicOnlyHTTPHandler(),
            PublicOnlyHTTPSHandler(context=ssl_context),
            NoRedirectHandler(),
        )

    def send(
        self,
        url: str,
        policy: FetchPolicy,
        cancel_event: threading.Event | None = None,
    ) -> RawResponse:
        raise_if_cancelled(cancel_event)
        deadline = FetchDeadline.current()
        try:
            request = urllib.request.Request(
                encode_url_as_ascii(url), headers=build_request_headers(policy)
            )
            response = self._open(request, policy.timeout)
        except (
            urllib.error.URLError,
            OSError,
            http.client.HTTPException,
            ValueError,
        ) as error:
            expired = deadline is not None and deadline.expired
            raise TransportError(
                classify_network_error(error, expired), describe(error)
            ) from error
        with closing(response):
            status = response.getcode()
            if not isinstance(status, int):
                raise TransportError(
                    FetchError.NETWORK_ERROR,
                    "Response did not provide a valid HTTP status code",
                )
            headers = normalize_headers(response.headers.items())
            body = BodyRead(b"")
            if should_read_body(status, headers, policy):
                body = read_response_capped(
                    response,
                    policy.max_page_bytes + 1,  # one extra byte reveals overflow
                    2 * policy.timeout,
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
        return RawResponse(status, headers, body.data, body.error)

    def _open(self, request: urllib.request.Request, timeout: float):
        try:
            return self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return error  # an HTTP error status is still a response
