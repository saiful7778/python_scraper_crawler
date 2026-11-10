import pytest

from app.services.crawler.extractors.business import find_business
from app.services.crawler.findings import Finding, parse_page

URL = "https://a.de/"


def _parse(html):
    return parse_page(html, URL)


def test_business_reads_json_ld():
    html = '<script type="application/ld+json">{"@type":"Bakery","name":"Bäckerei Schmidt","openingHours":"Mo-Fr 07:00-18:00"}</script>'
    rows = find_business(parse_page(html, "https://a.de/"), "https://a.de/")
    assert (
        Finding("business", "name", "Bäckerei Schmidt", extra={"schema_type": "Bakery"})
        in rows
    )
    assert (
        Finding(
            "business",
            "opening_hours",
            "Mo-Fr 07:00-18:00",
            extra={"schema_type": "Bakery"},
        )
        in rows
    )


def test_business_reads_organization_fields_from_graph():
    html = """<script type="application/ld+json">
    {"@graph":[{"@type":"WebSite","name":"Site"},
      {"@type":"Organization","name":"Acme","priceRange":"$$",
       "logo":{"@type":"ImageObject","url":"https://a.de/logo.png"},
       "foundingDate":"1999"}]}</script>"""
    rows = find_business(_parse(html), URL)
    extra = {"schema_type": "Organization"}
    assert Finding("business", "name", "Acme", extra=extra) in rows
    assert Finding("business", "price_range", "$$", extra=extra) in rows
    assert Finding("business", "logo_url", "https://a.de/logo.png", extra=extra) in rows
    assert Finding("business", "founded", "1999", extra=extra) in rows
    assert all(row.value != "Site" for row in rows)


def test_malformed_json_ld_block_does_not_hide_the_others():
    html = """<script type="application/ld+json">{not json</script>
      <script type="application/ld+json">{"@type":"Restaurant","name":"Gut"}</script>"""
    rows = find_business(_parse(html), URL)
    assert (
        Finding("business", "name", "Gut", extra={"schema_type": "Restaurant"}) in rows
    )

@pytest.mark.parametrize(
    "schema_type",
    [
        "VeterinaryCare",
        "Hospital",
        "GeneralContractor",
        "BankOrCreditUnion",
        "GovernmentOrganization",
        "MedicalOrganization",
        "TattooParlor",
        "NightClub",
    ],
)
def test_business_recognises_less_common_schema_subtypes(schema_type):
    html = (
        '<script type="application/ld+json">'
        f'{{"@type":"{schema_type}","name":"Acme"}}</script>'
    )
    rows = find_business(_parse(html), URL)
    assert rows == [
        Finding("business", "name", "Acme", extra={"schema_type": schema_type})
    ]


def test_business_ignores_non_business_schema_types():
    html = (
        '<script type="application/ld+json">{"@type":"WebSite","name":"Acme"}</script>'
    )
    assert find_business(_parse(html), URL) == []
