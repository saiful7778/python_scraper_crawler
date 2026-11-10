"""The one duplicate check: search, resume and merging across grid cells all use it."""

from app.repositories.google_map_profile_repository import KEY_FIELDS

from .scrape_types import LeadRow


def lead_keys(row: LeadRow) -> list[str]:
    """The keys that identify a lead. Two leads sharing any key are duplicates."""
    return [f"{field}:{row[field]}" for field in KEY_FIELDS if row.get(field)]


def add_if_new(row: LeadRow, seen: set[str]) -> bool:
    """Record the lead's keys in ``seen`` and return True, or return False if it was already known."""
    keys = lead_keys(row)
    if any(key in seen for key in keys):
        return False
    seen.update(keys)
    return True


def seen_keys_of(rows: list[LeadRow]) -> set[str]:
    """The keys of rows already stored, for resuming a run."""
    seen: set[str] = set()
    for row in rows:
        add_if_new(row, seen)
    return seen


def dedupe_rows(
    rows: list[LeadRow], known_keys: frozenset[str] = frozenset()
) -> list[LeadRow]:
    """Keep the first row of each lead, dropping any lead whose keys are in ``known_keys``."""
    seen: set[str] = set(known_keys)
    return [row for row in rows if add_if_new(row, seen)]
