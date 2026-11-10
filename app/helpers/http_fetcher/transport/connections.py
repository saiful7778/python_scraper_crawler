"""urllib connection and handler classes that refuse non-public peers.

Every connection created here is checked against the public-address guard and
armed with the hard stop as soon as it connects.
"""

import http.client
import urllib.request
from typing import cast

from app.utils.network_guard import create_public_connection

from ..hard_stop import FetchDeadline


class _GuardedConnection:
    """Mixin: refuse non-public peers and arm the hard stop once connected."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = create_public_connection

    def connect(self) -> None:
        super().connect()
        FetchDeadline.arm(cast(http.client.HTTPConnection, self))


class PublicOnlyHTTPConnection(_GuardedConnection, http.client.HTTPConnection):
    """HTTP connection that refuses non-public peers and arms the hard stop."""


class PublicOnlyHTTPSConnection(_GuardedConnection, http.client.HTTPSConnection):
    """HTTPS connection that refuses non-public peers and arms the hard stop."""


class PublicOnlyHTTPHandler(urllib.request.HTTPHandler):
    """urllib handler that opens :class:`PublicOnlyHTTPConnection`."""

    def http_open(self, req):
        return self.do_open(PublicOnlyHTTPConnection, req)


class PublicOnlyHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib handler that opens :class:`PublicOnlyHTTPSConnection`."""

    def __init__(self, debuglevel=0, context=None, check_hostname=None):
        super().__init__(
            debuglevel=debuglevel,
            context=context,
            check_hostname=check_hostname,
        )
        self._ssl_context = context

    def https_open(self, req):
        return self.do_open(PublicOnlyHTTPSConnection, req, context=self._ssl_context)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Surfaces redirects as responses so the fetcher can vet every hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
