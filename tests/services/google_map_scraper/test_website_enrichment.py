from app.services.google_map_scraper.job.website_enrichment import (
    ContactOptions,
    WebsiteEnricher,
    contact_fields,
    lacks_contacts,
)
from app.utils.failure_messages import is_proxy_failure, is_timeout_failure

PAGE = (
    '<a href="mailto:hi@bakery.studio.de">mail</a>'
    '<a href="https://www.instagram.com/acme_bakery/">ig</a>'
)


def test_contact_fields_reads_only_the_wanted_fields():
    options = ContactOptions(emails=True, socials=False, use_browser=False)
    fields = contact_fields(PAGE, "https://bakery.studio.de", options)
    assert fields == {"emails": "hi@bakery.studio.de"}


def test_contact_fields_of_an_empty_page_is_empty():
    options = ContactOptions(emails=True, socials=True, use_browser=False)
    assert contact_fields("", "https://x.example", options) == {}


def test_lacks_contacts_checks_only_wanted_fields():
    options = ContactOptions(emails=False, socials=True, use_browser=False)
    assert lacks_contacts({"emails": ""}, options)
    assert not lacks_contacts({"instagram": "https://instagram.com/a"}, options)


def test_enricher_fills_rows_from_fetched_pages():
    def fake_fetch(url, timeout):
        return PAGE if "bakery" in url else ""

    rows = [
        {"website": "https://bakery.studio.de", "title": "Bakery"},
        {"website": "https://nothing.example", "title": "Empty"},
        {"website": "", "title": "No site"},
    ]
    options = ContactOptions(emails=True, socials=True, use_browser=False)
    result = WebsiteEnricher(fetch_page=fake_fetch).enrich(
        rows, seconds=5, options=options, should_stop=lambda: False
    )

    assert result.rows_enriched == 1
    assert result.unchecked == 0
    assert rows[0]["emails"] == "hi@bakery.studio.de"
    assert rows[0]["instagram"] == "https://www.instagram.com/acme_bakery"
    assert "emails" not in rows[1]


def test_failure_classifiers_recognise_proxy_and_timeout_messages():
    assert is_proxy_failure("net::ERR_PROXY_CONNECTION_FAILED")
    assert is_timeout_failure("Navigation timed out after 45 s")
    assert not is_proxy_failure("connection lost")
    assert not is_timeout_failure("connection lost")
