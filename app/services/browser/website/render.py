"""The entry point for rendering one URL in a headless browser."""

from collections.abc import Callable

from .constants import DEFAULT_RENDER_SECONDS
from .render_budget import RenderBudget
from .render_result import RenderResult


def render_website(
    url: str,
    is_public_ip: Callable[[str], bool],
    timeout: float = DEFAULT_RENDER_SECONDS,
) -> RenderResult:
    """Render ``url`` and return its HTML and status.

    Blocks until the page finishes, fails, or the timeout runs out. Every
    connection the browser makes goes through a proxy that refuses
    non-public addresses.

    :param url: An http(s) URL, or a bare host, which is treated as HTTPS.
    :param is_public_ip: Predicate deciding whether an IP address is public.
    :param timeout: Seconds allowed for the whole render.
    """
    with RenderBudget(timeout) as budget:
        return budget.fetch(url, is_public_ip)
