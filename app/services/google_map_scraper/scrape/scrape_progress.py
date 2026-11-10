"""Progress bookkeeping that is persisted so an interrupted scrape can resume."""

import time
from typing import Unpack

from .scrape_store import ScrapeStore
from .scrape_types import LeadRow, ProgressData


class ScrapeProgress:
    """The progress dict of one scrape, saved to the store on every change.

    Each save also records the elapsed seconds (including time used by earlier
    runs of a resumed scrape) and the number of leads found so far.
    """

    def __init__(
        self,
        store: ScrapeStore,
        scrape_id: str,
        initial: ProgressData,
        rows: list[LeadRow],
        started_at: float,
        seconds_used_before: float,
    ):
        """Wrap ``initial`` progress; ``rows`` is the live list of result rows."""
        self.data: ProgressData = initial.copy()
        self._store = store
        self._scrape_id = scrape_id
        self._rows = rows
        self._started_at = started_at
        self._seconds_used_before = seconds_used_before

    def elapsed_seconds(self) -> float:
        """Total seconds spent on the scrape across all runs, rounded to 0.1 s."""
        return round(self._seconds_used_before + time.monotonic() - self._started_at, 1)

    def stamp(self, **fields: Unpack[ProgressData]) -> None:
        """Update fields (plus elapsed time and lead count) without saving."""
        self.data.update(fields)
        self.data["elapsed"] = self.elapsed_seconds()
        self.data["leads"] = len(self._rows)

    def save(self, **fields: Unpack[ProgressData]) -> None:
        """Update fields and persist the progress dict."""
        self.stamp(**fields)
        self._store.save_progress(self._scrape_id, self.data)
