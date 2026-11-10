"""Site-wide checks that are run once per crawl, not once per page."""

from ..findings import Finding


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def find_site_checks(
    homepage_status: int,
    final_url: str,
    response_ms: int,
    robots_text: str | None,
    sitemap_ok: bool,
    sitemap_url_count: int,
) -> list[Finding]:
    return [
        Finding(
            "site", "robots_txt", _yes_no(bool(robots_text and robots_text.strip()))
        ),
        Finding("site", "sitemap_xml", _yes_no(sitemap_ok)),
        Finding("site", "sitemap_url_count", str(sitemap_url_count)),
        Finding("site", "https", _yes_no(final_url.lower().startswith("https://"))),
        Finding("site", "http_status_home", str(homepage_status)),
        Finding("site", "response_time_ms", str(response_ms)),
    ]
