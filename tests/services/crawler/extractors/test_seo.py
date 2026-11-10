from app.services.crawler.extractors.seo import find_seo
from app.services.crawler.findings import Finding, parse_page

URL = "https://a.de/"


def _parse(html):
    return parse_page(html, URL)


def test_seo_reads_title_meta_and_structured_types():
    html = """<html><head><title>Bakery</title>
      <meta name="description" content="Fresh bread">
      <link rel="canonical" href="https://a.de/">
      <script type="application/ld+json">{"@type":"WebSite"}</script></head>
      <body><h1>Welcome</h1><img src="a.jpg"><img src="b.jpg" alt="x"></body></html>"""
    rows = find_seo(parse_page(html, "https://a.de/"), "https://a.de/")
    assert Finding("seo", "title", "Bakery") in rows
    assert Finding("seo", "meta_description", "Fresh bread") in rows
    assert Finding("seo", "h1", "Welcome") in rows
    assert Finding("seo", "structured_data_type", "WebSite") in rows
    assert Finding("seo", "image_missing_alt_count", "1") in rows


def test_seo_reads_social_meta_robots_and_link_counts():
    html = """<head><meta name="robots" content="noindex">
      <meta property="og:title" content="T"><meta property="og:description" content="D">
      <meta property="og:image" content="/i.png"><meta name="twitter:card" content="summary">
      <link rel="canonical" href="/home"></head>
      <body><p>one two three</p>
      <a href="/about">a</a><a href="https://www.a.de/b">b</a>
      <a href="https://other.com/">c</a><a href="mailto:x@y.de">d</a><a href="#top">e</a></body>"""
    rows = find_seo(_parse(html), URL)
    assert Finding("seo", "robots_meta", "noindex") in rows
    assert Finding("seo", "og_title", "T") in rows
    assert Finding("seo", "og_description", "D") in rows
    assert Finding("seo", "og_image", "https://a.de/i.png") in rows
    assert Finding("seo", "twitter_card", "summary") in rows
    assert Finding("seo", "canonical_url", "https://a.de/home") in rows
    assert Finding("seo", "word_count", "8") in rows
    assert Finding("seo", "internal_link_count", "2") in rows
    assert Finding("seo", "external_link_count", "1") in rows


def test_seo_returns_nothing_for_an_empty_page():
    assert find_seo(_parse("<html></html>"), URL) == []
