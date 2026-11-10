from app.services.crawler.extractors.social import find_social
from app.services.crawler.findings import Finding, parse_page

URL = "https://a.de/"


def _parse(html):
    return parse_page(html, URL)


def test_social_finds_instagram_link():
    html = '<a href="https://www.instagram.com/bakery_x/">ig</a>'
    rows = find_social(_parse(html), URL)
    assert Finding("social", "instagram", "https://www.instagram.com/bakery_x") in rows
    assert all(row.key != "facebook" for row in rows)
