"""Pure redirect planning: follow a hop only when it is safe and in scope."""

import urllib.parse
from collections.abc import Mapping, Sequence

from app.utils.url_utils import normalize_url, same_scope, validate_url

from .errors import FetchError
from .models import FetchPolicy, RedirectPlan


def plan_redirect(
    trail: Sequence[str],
    headers: Mapping[str, str],
    policy: FetchPolicy,
    *,
    scope: str | None = None,
    allow_subdomains: bool = False,
) -> RedirectPlan:
    """Decide whether to follow a redirect answered at ``trail[-1]``.

    ``trail`` holds every URL requested so far, starting with the first one.
    """
    if len(trail) - 1 >= policy.max_redirects:
        return RedirectPlan(error=FetchError.REDIRECT_LIMIT)
    location = (headers.get("location") or "").strip()
    if not location:
        return RedirectPlan(error=FetchError.INVALID_REDIRECT)
    try:
        joined = urllib.parse.urljoin(trail[-1], location)
        validate_url(joined)
        destination = normalize_url(joined)
    except ValueError:
        return RedirectPlan(error=FetchError.UNSAFE_REDIRECT)
    if destination in trail:
        return RedirectPlan(error=FetchError.REDIRECT_LOOP)
    if scope is not None and not same_scope(destination, scope, allow_subdomains):
        return RedirectPlan(error=FetchError.REDIRECT_OUT_OF_SCOPE)
    return RedirectPlan(url=destination)
