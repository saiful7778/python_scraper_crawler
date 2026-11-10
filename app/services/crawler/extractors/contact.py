"""Finds emails, phone numbers, addresses, contact forms and WhatsApp links on one page."""

import urllib.parse

from app.utils.errors import describe
from app.utils.logger import get_logger

from ..document import Document, absolute
from ..findings import Finding, json_ld_items, schema_types
from .email import find_emails

logger = get_logger(__name__)

WHATSAPP_HOSTS = ("wa.me", "api.whatsapp.com")
ADDRESS_PARTS = ("streetAddress", "postalCode", "addressLocality", "addressCountry")


def _postal_addresses(node):
    if isinstance(node, dict):
        if "PostalAddress" in schema_types(node):
            yield node
        for value in node.values():
            yield from _postal_addresses(value)
    elif isinstance(node, list):
        for value in node:
            yield from _postal_addresses(value)


def _address_text(node: dict) -> str:
    parts = []
    for name in ADDRESS_PARTS:
        value = node.get(name)
        if isinstance(value, dict):
            value = value.get("name")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return ", ".join(parts)


def _has_contact_form(doc: Document) -> bool:
    in_form = False
    for item in doc.elements:
        if item["tag"] == "form":
            in_form = True
        elif in_form and (
            item["tag"] == "textarea"
            or (
                item["tag"] == "input"
                and (item["attrs"].get("type") or "").lower() == "email"
            )
        ):
            return True
    return False


def find_contact(doc: Document, page_url: str) -> list[Finding]:
    rows: list[Finding] = []
    hrefs = [
        item["attrs"]["href"].strip()
        for item in doc.elements
        if item["tag"] == "a" and item["attrs"].get("href")
    ]

    mailto = [
        href[7:].split("?", 1)[0]
        for href in hrefs
        if href.lower().startswith("mailto:")
    ]
    for email in find_emails(" ".join([*mailto, *doc.text])):
        rows.append(Finding("contact", "email", email))

    phones: list[str] = []
    for href in hrefs:
        if href.lower().startswith("tel:"):
            phone = "".join(urllib.parse.unquote(href[4:]).split())
            if phone and phone not in phones:
                phones.append(phone)
    rows.extend(Finding("contact", "phone", phone) for phone in phones)

    for node in json_ld_items(doc):
        for address in _postal_addresses(node):
            text = _address_text(address)
            finding = Finding("contact", "address", text)
            if text and finding not in rows:
                rows.append(finding)

    if _has_contact_form(doc):
        rows.append(Finding("contact", "contact_form", "yes"))

    for href in hrefs:
        try:
            target = absolute(page_url, href)
            host = urllib.parse.urlsplit(target).netloc.lower()
        except ValueError as error:
            logger.debug(
                "skipped malformed link %r on %s: %s", href, page_url, describe(error)
            )
            continue
        if host in WHATSAPP_HOSTS:
            finding = Finding("contact", "whatsapp", target)
            if finding not in rows:
                rows.append(finding)
    return rows
