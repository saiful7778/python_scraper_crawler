"""Progress bookkeeping that is persisted so an interrupted scrape can resume."""

import time
from typing import Unpack

from redis.exceptions import RedisError

from app.services.buffer.client import BufferError
from app.services.buffer.scrape_state import ScrapeState
from app.utils.errors import describe

from .scrape_types import LeadRow, ProgressData


class ScrapeProgress:
    """The progress dict of one scrape, saved to Redis on every change.

    Each save also records the elapsed seconds (including time used by earlier
    runs of a resumed scrape) and the number of leads found so far.
    """

    def __init__(
        self,
        state: ScrapeState,
        scrape_id: str,
        initial: ProgressData,
        rows: list[LeadRow],
        started_at: float,
        seconds_used_before: float,
        offset: int = 0,
    ):
        """Wrap ``initial`` progress; ``rows`` is the live list of result rows.

        ``offset`` counts the leads of earlier runs that are no longer in ``rows``.
        """
        self.data: ProgressData = initial.copy()
        self._state = state
        self._scrape_id = scrape_id
        self._rows = rows
        self._started_at = started_at
        self._seconds_used_before = seconds_used_before
        self._offset = offset

    def elapsed_seconds(self) -> float:
        """Total seconds spent on the scrape across all runs, rounded to 0.1 s."""
        return round(self._seconds_used_before + time.monotonic() - self._started_at, 1)

    def stamp(self, **fields: Unpack[ProgressData]) -> None:
        """Update fields (plus elapsed time and lead count) without saving."""
        self.data.update(fields)
        self.data["elapsed"] = self.elapsed_seconds()
        self.data["leads"] = self._offset + len(self._rows)

    def save(self, **fields: Unpack[ProgressData]) -> None:
        """Update fields and persist the progress dict.

        :raises BufferError: If Redis fails; the scrape must stop, not go on unsaved.
        """
        self.stamp(**fields)
        try:
            self._state.save_progress(self._scrape_id, dict(self.data))
        except RedisError as error:
            raise BufferError(
                f"Redis failed while saving scrape progress: {describe(error)}"
            ) from error
