"""Phone number checks for scraped leads, using the scrape's country."""

import phonenumbers

from .scrape_types import LeadRow


def format_phone_number(row: LeadRow, country: str) -> None:
    """Replace a row's phone with its E.164 form, or ``None`` when it is not valid.

    The country is the region Google's result is assumed to be from, so a
    number without a country code (``512-555-0123``) is read in that region.
    """
    raw = str(row.get("phone") or "").strip()
    if not raw:
        return
    try:
        number = phonenumbers.parse(raw, country.upper() or None)
    except phonenumbers.NumberParseException:
        row["phone"] = None
        return
    row["phone"] = (
        phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
        if phonenumbers.is_valid_number(number)
        else None
    )


def format_phone_numbers(rows: list[LeadRow], country: str) -> None:
    """Format every row's phone for the country."""
    for row in rows:
        format_phone_number(row, country)
