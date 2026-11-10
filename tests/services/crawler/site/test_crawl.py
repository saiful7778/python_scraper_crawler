from dataclasses import replace

import pytest

from app.helpers.http_fetcher.errors import FetchError
from app.helpers.http_fetcher.models import build_result
from app.services.crawler.site import crawl as website_crawl
from app.services.crawler.site.crawl import CrawlOptions, WebsiteCrawl
from app.services.crawler.storage.buffer import CrawlBuffer
from app.services.crawler.storage.crawl_store import CrawlStore
from app.services.google_map_scraper.scrape.scrape_models import ScrapeLeadModel
from app.services.google_map_scraper.scrape.scrape_settings import ScrapeSettings
from app.services.google_map_scraper.scrape.scrape_store import ScrapeStore
from app.services.google_map_scraper.scrape.scrape_types import LeadRow

OPTS = CrawlOptions(
    max_pages=100,
    max_depth=3,
    store_html=True,
    collect_emails=True,
    collect_socials=True,
)
ORIGIN = "https://bakery.de"
HTML = {"content-type": "text/html; charset=utf-8"}


def _lead() -> LeadRow:
    row: dict[str, object] = {key: "" for key in LeadRow.__required_keys__}
    row["latitude"] = None
    row["longitude"] = None
    row["title"] = "Bakery"
    row["website"] = ORIGIN
    return row  # type: ignore[return-value]


async def _first_lead_id(scrape_id: str) -> int:
    lead = await ScrapeLeadModel.filter(scrape_id=scrape_id).first()
    assert lead is not None
    return lead.id


@pytest.fixture
def store(tmp_path):
    scrape_store = ScrapeStore(str(tmp_path))
    yield scrape_store
    scrape_store.close()


@pytest.fixture
def buffer(store):
    return CrawlBuffer(CrawlStore(store))


@pytest.fixture
def website_id(store, buffer):
    settings = ScrapeSettings(keywords=("bakery",))
    scrape_id = store.create_scrape(
        "t", settings.model_dump(by_alias=True, mode="json")
    )
    store.append_rows(scrape_id, [_lead()])
    lead_id = store.run_async(_first_lead_id(scrape_id))
    return buffer.create_website(
        scrape_id=scrape_id,
        lead_position=0,
        scrape_lead_id=lead_id,
        host="bakery.de",
        registrable_domain="bakery.de",
        origin=ORIGIN,
    )


class Site:
    scrape_id = "scrape-1"

    def __init__(self):
        self.results = {}
        self.fetched_urls = []

    def page(self, path, body="<html></html>", status=200, headers=None, raw=None):
        url = ORIGIN + path
        data = raw if raw is not None else body.encode()
        self.results[url] = build_result(
            trail=[url], status=status, headers=headers or HTML, body=data
        )

    def put(self, url, result):
        self.results[url] = result

    def fetch(self, url):
        self.fetched_urls.append(url)
        if isinstance(self.results.get(url), Exception):
            raise self.results[url]
        return self.results.get(url) or build_result(trail=[url], status=404)


HOME_HTML = """<html><head><title>Bakery</title>
<link rel="canonical" href="https://bakery.de/"></head><body>
<a href="/contact">Contact</a>
<a href="/about#team">About</a>
<a href="https://facebook.com/bakery">Facebook</a>
<a href="mailto:hi@bakery.de">mail</a>
</body></html>"""


@pytest.fixture
def site():
    s = Site()
    s.page("/", HOME_HTML)
    s.page(
        "/contact", '<html><body><a href="mailto:info@bakery.de">m</a></body></html>'
    )
    s.page("/about", "<html><title>About</title></html>")
    return s


def make_crawl(buffer, website_id, site, options=OPTS):
    return WebsiteCrawl(
        buffer=buffer,
        fetch=site.fetch,
        website_id=website_id,
        scrape_id=site.scrape_id,
        lead_position=0,
        website="https://bakery.de",
        options=options,
    )


def test_happy_path_saves_pages_links_and_findings(buffer, website_id, site):
    crawl = make_crawl(buffer, website_id, site)
    assert crawl.run() == "done"
    snap = buffer.read_website(website_id)
    assert {p["normalized_url"] for p in snap.pages} >= {
        "https://bakery.de/",
        "https://bakery.de/contact",
    }
    assert any(f["key"] == "email" for f in snap.findings)
    assert any(
        f["group"] == "site" and f["key"] == "http_status_home" for f in snap.findings
    )
    assert snap.runs[0]["status"] == "done"
    assert snap.website["status"] == "done"
    assert crawl.run_id == str(snap.runs[0]["id"])
    assert len(snap.raw_html) >= 3
    home = next(p for p in snap.pages if p["normalized_url"] == "https://bakery.de/")
    assert home["title"] == "Bakery"
    assert home["status"] == "fetched"
    assert len(snap.links) >= 3


def test_html_is_not_stored_when_store_html_is_off(buffer, website_id, site):
    make_crawl(buffer, website_id, site, replace(OPTS, store_html=False)).run()
    assert buffer.read_website(website_id).raw_html == []


def test_failing_extractor_does_not_stop_the_others(
    buffer, website_id, site, monkeypatch
):
    monkeypatch.setattr(website_crawl, "find_seo", lambda doc, url: 1 / 0)
    crawl = make_crawl(buffer, website_id, site)
    assert crawl.run() == "partial"
    snap = buffer.read_website(website_id)
    assert any(
        e["step"] == "extract_seo" and e["level"] == "error" for e in snap.events
    )
    assert any(f["key"] == "email" for f in snap.findings)
    assert any(f["group"] == "site" for f in snap.findings)


def test_every_extractor_failing_still_finishes_the_run(
    buffer, website_id, site, monkeypatch
):
    for name in (
        "find_contact",
        "find_social",
        "find_tech",
        "find_business",
        "find_seo",
    ):
        monkeypatch.setattr(website_crawl, name, lambda doc, url: 1 / 0)
    assert make_crawl(buffer, website_id, site).run() == "partial"
    snap = buffer.read_website(website_id)
    steps = {e["step"] for e in snap.events}
    assert {
        "extract_contact",
        "extract_social",
        "extract_tech",
        "extract_business",
        "extract_seo",
    } <= steps
    assert len(snap.pages) >= 3


def test_collect_flags_switch_extractors_off(buffer, website_id, site, monkeypatch):
    calls = []
    monkeypatch.setattr(
        website_crawl, "find_social", lambda doc, url: calls.append(url) or []
    )
    options = replace(OPTS, collect_socials=False, collect_emails=False)
    make_crawl(buffer, website_id, site, options).run()
    assert calls == []
    assert not any(
        f["key"] == "email" for f in buffer.read_website(website_id).findings
    )


def test_site_step_failure_is_recorded_and_partial(
    buffer, website_id, site, monkeypatch
):
    monkeypatch.setattr(website_crawl, "find_site_checks", lambda *a, **k: 1 / 0)
    assert make_crawl(buffer, website_id, site).run() == "partial"
    snap = buffer.read_website(website_id)
    assert any(e["step"] == "site" and e["level"] == "error" for e in snap.events)
    assert len(snap.pages) >= 3


def test_homepage_failure_marks_run_failed(buffer, website_id):
    down = Site()
    down.put(
        ORIGIN + "/",
        build_result(trail=[ORIGIN + "/"], error=FetchError.NETWORK_ERROR),
    )
    crawl = make_crawl(buffer, website_id, down)
    assert crawl.run() == "failed"
    snap = buffer.read_website(website_id)
    assert snap.events[0]["step"] == "fetch_home"
    assert snap.runs[0]["status"] == "failed"
    assert down.fetched_urls == [ORIGIN + "/robots.txt", ORIGIN + "/"]


def test_homepage_http_error_marks_run_failed(buffer, website_id):
    broken = Site()
    broken.page("/", status=503)
    assert make_crawl(buffer, website_id, broken).run() == "failed"


def test_homepage_exception_marks_run_failed(buffer, website_id):
    broken = Site()
    broken.put(ORIGIN + "/", RuntimeError("boom"))
    assert make_crawl(buffer, website_id, broken).run() == "failed"
    assert buffer.read_website(website_id).events[0]["step"] == "fetch_home"


def test_homepage_redirect_off_domain_is_not_followed(buffer, website_id):
    redirect = Site()
    redirect.put(
        ORIGIN + "/",
        build_result(
            trail=[ORIGIN + "/", "https://other.com/"],
            status=200,
            headers=HTML,
            body=b"<html><a href='/x'>x</a></html>",
        ),
    )
    assert make_crawl(buffer, website_id, redirect).run() == "partial"
    assert all("other.com" not in u for u in redirect.fetched_urls)
    pages = buffer.read_website(website_id).pages
    assert [p["skip_reason"] for p in pages] == ["out_of_scope"]


def test_homepage_redirect_to_www_is_followed(buffer, website_id):
    redirect = Site()
    redirect.put(
        ORIGIN + "/",
        build_result(
            trail=[ORIGIN + "/", "https://www.bakery.de/"],
            status=200,
            headers=HTML,
            body=b"<html><title>x</title></html>",
        ),
    )
    assert make_crawl(buffer, website_id, redirect).run() == "done"


def test_binary_and_oversized_pages_are_skipped(buffer, website_id):
    s = Site()
    s.page(
        "/",
        '<a href="/file.pdf">p</a><a href="/huge">h</a><a href="/notes.txt">t</a>',
    )
    s.page("/file.pdf", raw=b"%PDF", headers={"content-type": "application/pdf"})
    s.page("/huge", raw=b"x" * 2_000_001)
    s.page("/notes.txt", raw=b"hello", headers={"content-type": "text/plain"})
    assert make_crawl(buffer, website_id, s).run() == "partial"
    pages = buffer.read_website(website_id).pages
    reasons = {p["skip_reason"] for p in pages}
    assert {"not_html", "too_large"} <= reasons
    notes = next(p for p in pages if p["normalized_url"].endswith("/notes.txt"))
    assert notes["status"] == "fetched"


def test_only_not_html_skips_keep_the_run_done(buffer, website_id):
    s = Site()
    s.page("/", '<a href="/file.pdf">p</a>')
    s.page("/file.pdf", raw=b"%PDF", headers={"content-type": "application/pdf"})
    assert make_crawl(buffer, website_id, s).run() == "done"


def test_broken_pages_are_saved_failed_and_the_crawl_continues(buffer, website_id):
    s = Site()
    s.page("/", '<a href="/gone">g</a><a href="/err">e</a><a href="/ok">o</a>')
    s.page("/err", status=500)
    s.put(
        ORIGIN + "/ok",
        build_result(
            trail=[ORIGIN + "/ok"], status=200, headers=HTML, body=b"<p>ok</p>"
        ),
    )
    s.put(ORIGIN + "/gone", RuntimeError("boom"))
    make_crawl(buffer, website_id, s).run()
    by_url = {p["normalized_url"]: p for p in buffer.read_website(website_id).pages}
    assert by_url[ORIGIN + "/err"]["status"] == "failed"
    assert by_url[ORIGIN + "/err"]["error_code"] == "http_500"
    assert by_url[ORIGIN + "/gone"]["status"] == "failed"
    assert by_url[ORIGIN + "/ok"]["status"] == "fetched"


@pytest.fixture
def site_big():
    s = Site()
    links = "".join(f'<a href="/p{i}">p</a>' for i in range(9))
    s.page("/", '<a href="/private/x">x</a>' + links)
    s.page(
        "/robots.txt",
        "User-agent: *\nDisallow: /private",
        headers={"content-type": "text/plain"},
    )
    for i in range(9):
        s.page(f"/p{i}")
    s.page("/private/x")
    return s


def test_robots_disallow_and_page_limits_are_respected(buffer, website_id, site_big):
    opts = replace(OPTS, max_pages=3, max_depth=1)
    assert make_crawl(buffer, website_id, site_big, options=opts).run() == "partial"
    snap = buffer.read_website(website_id)
    assert len(snap.pages) <= 3
    assert ORIGIN + "/private/x" not in site_big.fetched_urls
    assert any(p["skip_reason"] == "robots_disallowed" for p in snap.pages)
    assert any(e["step"] == "pages" and e["level"] == "warning" for e in snap.events)


def test_robots_disallow_alone_keeps_the_run_done(buffer, website_id, site_big):
    assert make_crawl(buffer, website_id, site_big).run() == "done"
    pages = buffer.read_website(website_id).pages
    assert len(pages) == 11
    robots = [
        f for f in buffer.read_website(website_id).findings if f["key"] == "robots_txt"
    ]
    assert [f["value"] for f in robots] == ["yes"]


def test_max_depth_limits_how_deep_links_are_followed(buffer, website_id):
    s = Site()
    s.page("/", '<a href="/a">a</a>')
    s.page("/a", '<a href="/b">b</a>')
    s.page("/b", "<p>deep</p>")
    make_crawl(buffer, website_id, s, replace(OPTS, max_depth=1)).run()
    snap = buffer.read_website(website_id)
    by_url = {p["normalized_url"]: p for p in snap.pages}
    assert set(by_url) == {ORIGIN + "/", ORIGIN + "/a", ORIGIN + "/b"}
    assert by_url[ORIGIN + "/b"]["skip_reason"] == "max_depth"
    assert ORIGIN + "/b" not in s.fetched_urls


def test_link_beyond_max_depth_is_recorded_as_a_link(buffer, website_id):
    s = Site()
    s.page("/", '<a href="/a">a</a>')
    s.page("/a", '<a href="/b">b</a>')
    s.page("/b", "<p>deep</p>")
    make_crawl(buffer, website_id, s, replace(OPTS, max_depth=1)).run()
    snap = buffer.read_website(website_id)
    ids = {p["normalized_url"]: p["id"] for p in snap.pages}
    links = {(link["source_id"], link["target_id"]) for link in snap.links}
    assert (ids[ORIGIN + "/a"], ids[ORIGIN + "/b"]) in links
    assert ORIGIN + "/b" not in s.fetched_urls


def test_robots_disallowing_the_homepage_means_it_is_never_requested(
    buffer, website_id
):
    s = Site()
    s.page("/", "<html><title>x</title></html>")
    s.page(
        "/robots.txt",
        "User-agent: *\nDisallow: /",
        headers={"content-type": "text/plain"},
    )
    assert make_crawl(buffer, website_id, s).run() == "partial"
    assert ORIGIN + "/" not in s.fetched_urls
    snap = buffer.read_website(website_id)
    assert [p["skip_reason"] for p in snap.pages] == ["robots_disallowed"]


def test_one_failing_sitemap_keeps_the_others(buffer, website_id):
    s = Site()
    s.page("/")
    s.page(
        "/robots.txt",
        f"Sitemap: {ORIGIN}/a.xml\nSitemap: {ORIGIN}/b.xml\n",
        headers={"content-type": "text/plain"},
    )
    s.put(ORIGIN + "/a.xml", RuntimeError("boom"))
    s.page(
        "/b.xml",
        f"<urlset><url><loc>{ORIGIN}/menu</loc></url></urlset>",
        headers={"content-type": "application/xml"},
    )
    s.page("/menu")
    assert make_crawl(buffer, website_id, s).run() == "partial"
    snap = buffer.read_website(website_id)
    assert any(e["step"] == "sitemap" and e["level"] == "error" for e in snap.events)
    menu = next(p for p in snap.pages if p["normalized_url"] == ORIGIN + "/menu")
    assert menu["status"] == "fetched"


def test_failing_email_filter_leaves_the_page_fetched(
    buffer, website_id, site, monkeypatch
):
    class BadRow:
        @property
        def group(self):
            raise RuntimeError("bad row")

    monkeypatch.setattr(website_crawl, "find_contact", lambda doc, url: [BadRow()])
    options = replace(OPTS, collect_emails=False)
    assert make_crawl(buffer, website_id, site, options).run() == "partial"
    snap = buffer.read_website(website_id)
    home = next(p for p in snap.pages if p["normalized_url"] == ORIGIN + "/")
    assert home["status"] == "fetched"
    assert any(
        e["step"] == "extract_contact" and e["level"] == "error" for e in snap.events
    )


def test_external_links_are_recorded_but_never_fetched(buffer, website_id, site):
    make_crawl(buffer, website_id, site).run()
    assert not any("facebook.com" in u for u in site.fetched_urls)
    pages = buffer.read_website(website_id).pages
    assert any(p["skip_reason"] == "out_of_scope" for p in pages)


def test_sitemap_urls_are_seeded_at_depth_one(buffer, website_id):
    s = Site()
    s.page("/")
    s.page(
        "/robots.txt",
        f"Sitemap: {ORIGIN}/map.xml\n",
        headers={"content-type": "text/plain"},
    )
    s.page(
        "/map.xml",
        f"<urlset><url><loc>{ORIGIN}/menu</loc></url>"
        "<url><loc>https://elsewhere.com/x</loc></url></urlset>",
        headers={"content-type": "application/xml"},
    )
    s.page("/menu")
    assert make_crawl(buffer, website_id, s).run() == "done"
    snap = buffer.read_website(website_id)
    menu = next(p for p in snap.pages if p["normalized_url"] == ORIGIN + "/menu")
    assert menu["discovery_source"] == "sitemap"
    assert menu["depth"] == 1
    assert menu["status"] == "fetched"
    assert not any("elsewhere.com" in u for u in s.fetched_urls)
    values = {f["key"]: f["value"] for f in snap.findings if f["group"] == "site"}
    assert values["sitemap_xml"] == "yes"
    assert values["sitemap_url_count"] == "2"


def test_sitemap_index_is_followed_one_level_and_capped_at_five(buffer, website_id):
    s = Site()
    s.page("/")
    children = "".join(
        f"<sitemap><loc>{ORIGIN}/s{i}.xml</loc></sitemap>" for i in range(8)
    )
    s.page("/sitemap.xml", f"<sitemapindex>{children}</sitemapindex>")
    for i in range(8):
        s.page(
            f"/s{i}.xml",
            f"<urlset><url><loc>{ORIGIN}/n{i}</loc></url></urlset>",
        )
        s.page(f"/n{i}")
    make_crawl(buffer, website_id, s).run()
    fetched_maps = [u for u in s.fetched_urls if "/s" in u and "/sitemap" not in u]
    assert len(fetched_maps) == 5


def test_invalid_sitemap_does_not_stop_the_crawl(buffer, website_id, site):
    site.page("/sitemap.xml", "<urlset", headers={"content-type": "application/xml"})
    make_crawl(buffer, website_id, site).run()
    snap = buffer.read_website(website_id)
    assert len(snap.pages) >= 3
    values = {f["key"]: f["value"] for f in snap.findings if f["group"] == "site"}
    assert values["sitemap_xml"] == "no"


def test_robots_step_failure_is_isolated(buffer, website_id, site):
    site.put(ORIGIN + "/robots.txt", RuntimeError("boom"))
    assert make_crawl(buffer, website_id, site).run() == "partial"
    snap = buffer.read_website(website_id)
    assert any(e["step"] == "robots" and e["level"] == "error" for e in snap.events)
    assert len(snap.pages) >= 3
    assert any(f["group"] == "site" for f in snap.findings)


def test_sitemap_step_failure_is_isolated(buffer, website_id, site, monkeypatch):
    monkeypatch.setattr(website_crawl, "sitemap_urls", lambda body: 1 / 0)
    site.page("/sitemap.xml", "<urlset></urlset>")
    assert make_crawl(buffer, website_id, site).run() == "partial"
    snap = buffer.read_website(website_id)
    assert any(e["step"] == "sitemap" and e["level"] == "error" for e in snap.events)
    assert len(snap.pages) >= 3


def test_page_saving_failure_does_not_stop_other_pages(
    buffer, website_id, site, monkeypatch
):
    real = buffer.add_findings

    def flaky(*, website_id, run_id, page_id, findings):
        if findings and any(f.key == "email" for f in findings):
            raise RuntimeError("disk")
        real(website_id=website_id, run_id=run_id, page_id=page_id, findings=findings)

    monkeypatch.setattr(buffer, "add_findings", flaky)
    assert make_crawl(buffer, website_id, site).run() == "partial"
    snap = buffer.read_website(website_id)
    assert any(f["group"] == "site" for f in snap.findings)
    assert any(e["level"] == "error" for e in snap.events)


def test_run_never_raises_when_the_buffer_fails(buffer, website_id, site, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(buffer, "create_run", broken)
    crawl = make_crawl(buffer, website_id, site)
    assert crawl.run() == "failed"
    assert crawl.run_id is None


def test_raising_homepage_fetch_is_recorded_as_a_failed_page(buffer, website_id, site):
    site.put(ORIGIN + "/", RuntimeError("connection reset"))
    crawl = make_crawl(buffer, website_id, site)
    assert crawl.run() == "failed"
    snap = buffer.read_website(website_id)
    home = next(p for p in snap.pages if p["normalized_url"] == "https://bakery.de/")
    assert home["status"] == "failed"
    assert home["error_code"] == "fetch_exception"
    assert "connection reset" in home["error_message"]
    assert not any(p["status"] == "queued" for p in snap.pages)
    assert any(e["step"] == "fetch_home" and e["level"] == "error" for e in snap.events)
    assert snap.runs[0]["error_code"] == "fetch_exception"


def test_malformed_link_does_not_drop_the_other_contacts(buffer, website_id, site):
    site.page(
        "/",
        '<html><body><a href="http://[broken">x</a>'
        '<a href="mailto:hi@bakery.de">m</a></body></html>',
    )
    assert make_crawl(buffer, website_id, site).run() in {"done", "partial"}
    snap = buffer.read_website(website_id)
    assert any(
        f["key"] == "email" and f["value"] == "hi@bakery.de" for f in snap.findings
    )
