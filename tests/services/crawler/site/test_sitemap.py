import pytest

from app.services.crawler.site.sitemap import sitemap_urls


def test_sitemap_index_returns_nested_sitemaps():
    body = b"<sitemapindex><sitemap><loc>https://a.de/s1.xml</loc></sitemap></sitemapindex>"
    pages, nested = sitemap_urls(body)
    assert pages == [] and nested == ["https://a.de/s1.xml"]


def test_urlset_returns_pages():
    body = b"<urlset><url><loc>https://a.de/x</loc></url></urlset>"
    assert sitemap_urls(body) == (["https://a.de/x"], [])


def test_bad_xml_raises_value_error():
    with pytest.raises(ValueError):
        sitemap_urls(b"<urlset>")
