"""Plain-HTTP page fetching with SSRF protection, retries and hard time limits.

Layout
------
* ``constants``, ``errors``, ``models``: shared tunables, failure codes and
  the data types passed between layers.
* ``responses``, ``body``, ``urls``, ``redirects``, ``retry``: pure functions
  that make every decision (URL handling, redirect planning, retry timing,
  response verdicts).  They do no I/O and never read a clock.
* ``timing``, ``hard_stop``, ``pacing``: clocks, deadlines and per-host rate
  limiting.
* ``transport``: the only layer that touches the network.  The urllib
  transport ignores environment proxies, refuses non-public peers (also
  after redirects) and cuts a stalled connection after ``hard_timeout``
  seconds even when the server keeps the socket open.
* ``fetcher``: :class:`WebPageFetcher`, which wires the layers together.

``WebPageFetcher.fetch_result`` returns a structured :class:`FetchResult`;
``WebPageFetcher.fetch`` is the thin text-only wrapper.  Both accept a
``threading.Event`` that cancels the fetch (raising :class:`FetchCancelled`)
between attempts, during backoff and pacing waits, and while reading a body.
"""

from .constants import SITE_TIMEOUT
from .errors import FetchCancelled, FetchError
from .fetcher import WebPageFetcher
from .models import FetchPolicy, FetchResult
from .pacing import HostPacer, RateLimit
from .transport import UrllibTransport

__all__ = [
    "SITE_TIMEOUT",
    "FetchCancelled",
    "FetchError",
    "FetchPolicy",
    "FetchResult",
    "HostPacer",
    "RateLimit",
    "UrllibTransport",
    "WebPageFetcher",
]
