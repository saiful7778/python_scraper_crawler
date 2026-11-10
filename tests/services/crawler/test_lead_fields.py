from app.services.crawler.findings import Finding
from app.services.crawler.lead_fields import lead_columns, social_links


def test_emails_are_joined_and_unique():
    rows = [
        Finding("contact", "email", "a@x.de"),
        Finding("contact", "email", "a@x.de"),
        Finding("contact", "email", "b@x.de"),
    ]
    assert lead_columns(rows, collect_emails=True) == {"emails": "a@x.de, b@x.de"}


def test_socials_are_not_lead_columns():
    rows = [Finding("social", "instagram", "https://instagram.com/bakery")]
    assert lead_columns(rows, collect_emails=False) == {}


def test_every_distinct_social_is_returned_when_collected():
    rows = [
        Finding("social", "instagram", "https://instagram.com/bakery"),
        Finding("social", "instagram", "https://instagram.com/bakery_shop"),
        Finding("social", "instagram", "https://instagram.com/bakery"),
    ]
    assert social_links(rows, collect_socials=True) == [
        ("instagram", "https://instagram.com/bakery"),
        ("instagram", "https://instagram.com/bakery_shop"),
    ]
    assert social_links(rows, collect_socials=False) == []


def test_only_known_tech_columns_are_returned():
    rows = [Finding("tech", "cms", "wordpress"), Finding("tech", "framework", "React")]
    assert lead_columns(rows, collect_emails=False) == {"cms": "wordpress"}
