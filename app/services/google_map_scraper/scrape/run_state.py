"""Mutable facts gathered while one scrape run is in progress."""

from dataclasses import dataclass, field

from .scrape_types import LeadRow


@dataclass
class RunState:
    """Rows, errors and flags shared by the search, reviews and website passes."""

    rows: list[LeadRow] = field(default_factory=list[LeadRow])
    errors: list[str] = field(default_factory=list[str])
    notes: list[str] = field(default_factory=list[str])
    refused_proxies: set[str | None] = field(default_factory=set[str | None])
    blocked: bool = False
    retries: int = 0
    timed_out: bool = False

    @property
    def last_error(self) -> str:
        """The most recent error message, or ``""`` when there were none."""
        return self.errors[-1] if self.errors else ""
