from app.services.crawler.findings import Finding
from app.services.crawler.site.checks import find_site_checks


def test_site_checks_report_robots_and_https():
    rows = find_site_checks(200, "https://a.de/", 320, "User-agent: *", False, 0)
    assert Finding("site", "robots_txt", "yes") in rows
    assert Finding("site", "https", "yes") in rows
    assert Finding("site", "sitemap_xml", "no") in rows


def test_site_checks_report_status_timing_and_missing_robots():
    rows = find_site_checks(301, "http://a.de/", 90, None, True, 12)
    assert Finding("site", "robots_txt", "no") in rows
    assert Finding("site", "https", "no") in rows
    assert Finding("site", "sitemap_xml", "yes") in rows
    assert Finding("site", "sitemap_url_count", "12") in rows
    assert Finding("site", "http_status_home", "301") in rows
    assert Finding("site", "response_time_ms", "90") in rows
