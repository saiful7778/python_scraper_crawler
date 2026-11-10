from collections.abc import Iterable
from typing import get_args

from app.constant import SocialKey

from .extractors.tech import TECH_FIELDS
from .findings import Finding

SOCIAL_KEYS = frozenset(get_args(SocialKey))


def lead_columns(findings: Iterable[Finding], *, collect_emails: bool) -> dict[str, str]:
    emails: dict[str, None] = {}
    columns: dict[str, str] = {}
    for finding in findings:
        if collect_emails and finding.group == "contact" and finding.key == "email":
            emails.setdefault(finding.value)
        elif finding.key in TECH_FIELDS:
            columns.setdefault(finding.key, finding.value)
    if emails:
        columns["emails"] = ", ".join(emails)
    return columns


def social_links(
    findings: Iterable[Finding], *, collect_socials: bool
) -> list[tuple[str, str]]:
    """Every distinct social profile found, as ``(platform, url)`` pairs."""
    if not collect_socials:
        return []
    return list(
        dict.fromkeys(
            (finding.key, finding.value)
            for finding in findings
            if finding.key in SOCIAL_KEYS
        )
    )
