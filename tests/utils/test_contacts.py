from app.utils.contacts import find_emails, find_socials


def test_find_emails_puts_the_business_domain_first():
    page = "write to info@gmail.com or owner@shop.studio.de, also hello@shop.studio.de"
    emails = find_emails(page, site="shop.studio.de")
    assert emails == [
        "owner@shop.studio.de",
        "hello@shop.studio.de",
        "info@gmail.com",
    ]


def test_find_emails_drops_image_names_and_placeholder_domains():
    page = "logo@2x.png and me@example.com and real@studio.io"
    assert find_emails(page) == ["real@studio.io"]


def test_find_emails_unescapes_html_entities_and_lowercases():
    assert find_emails("write name&#64;Studio.IO today") == ["name@studio.io"]


def test_find_emails_keeps_at_most_three():
    page = " ".join(f"user{i}@studio.io" for i in range(5))
    assert len(find_emails(page)) == 3


def test_find_socials_picks_the_first_profile_per_network():
    page = (
        'href="https://www.instagram.com/acme_bakery/" '
        'href="https://www.instagram.com/other/" '
        'href="https://facebook.com/sharer/sharer.php"'
    )
    socials = find_socials(page)
    assert socials["instagram"] == "https://www.instagram.com/acme_bakery"
    assert socials["facebook"] == ""


def test_find_socials_skips_numeric_facebook_ids():
    assert find_socials("https://facebook.com/12345")["facebook"] == ""
    assert (
        find_socials("https://facebook.com/acmebakery")["facebook"]
        == "https://facebook.com/acmebakery"
    )
