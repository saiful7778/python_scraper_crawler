from app.services.crawler.extractors.tech import find_tech
from app.services.crawler.findings import Finding, parse_page

URL = "https://a.de/"


def _parse(html):
    return parse_page(html, URL)


def test_tech_finds_cms_and_language():
    html = """<html lang="de"><head>
      <script src="/wp-content/themes/x/app.js"></script></head></html>"""
    rows = find_tech(_parse(html), URL)
    assert Finding("tech", "cms", "wordpress") in rows
    assert Finding("tech", "language", "de") in rows


def test_tech_finds_frameworks_and_viewport():
    html = """<html><head><meta name="viewport" content="width=device-width"></head>
      <body><div id="__next"></div><script id="__NEXT_DATA__">{}</script></body></html>"""
    rows = find_tech(_parse(html), URL)
    assert Finding("tech", "framework", "Next.js") in rows
    assert Finding("tech", "mobile_friendly", "yes") in rows
