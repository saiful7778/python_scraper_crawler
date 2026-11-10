from app.services.crawler.extractors.contact import find_contact
from app.services.crawler.findings import Finding, parse_page

URL = "https://a.de/"


def _parse(html):
    return parse_page(html, URL)


def test_contact_finds_emails_phones_and_form():
    html = """<html><body>
      <a href="mailto:hi@bakery.studio.de">mail</a>
      <a href="tel:+4930123456">call</a>
      <form><input type="email"><textarea></textarea></form>
    </body></html>"""
    rows = find_contact(
        parse_page(html, "https://bakery.studio.de/"), "https://bakery.studio.de/"
    )
    assert Finding("contact", "email", "hi@bakery.studio.de") in rows
    assert Finding("contact", "phone", "+4930123456") in rows
    assert Finding("contact", "contact_form", "yes") in rows


def test_contact_drops_image_file_addresses():
    html = "<p>logo@2x.png</p>"
    assert find_contact(parse_page(html, "https://a.de/"), "https://a.de/") == []


def test_contact_reads_visible_text_whatsapp_and_postal_address():
    html = """<p>Write to info@bakery.de</p>
      <a href="https://wa.me/4930123456">chat</a>
      <script type="application/ld+json">
      {"@type":"Bakery","address":{"@type":"PostalAddress",
       "streetAddress":"Hauptstr. 1","postalCode":"10115","addressLocality":"Berlin"}}
      </script>"""
    rows = find_contact(_parse(html), URL)
    assert Finding("contact", "email", "info@bakery.de") in rows
    assert Finding("contact", "whatsapp", "https://wa.me/4930123456") in rows
    assert Finding("contact", "address", "Hauptstr. 1, 10115, Berlin") in rows


def test_contact_without_form_fields_has_no_contact_form():
    rows = find_contact(_parse("<form><input type='text'></form>"), URL)
    assert rows == []
