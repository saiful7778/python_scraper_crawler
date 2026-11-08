"""The reviews pass: reads the review panel of each lead found by the search."""

import threading
import time

from app.services.browser import BrowserSession, CDPError
from app.services.google_map_scraper.extract_details import (
    PanelRecord,
    parse_place_panel,
)
from app.services.google_map_scraper.google_map import (
    CONSENT_COOKIES,
    fetch_lead_panel_json,
)

from .job_progress import JobProgress
from .job_settings import JobSettings
from .job_types import LeadRow, is_lead_key

_PANEL_ONLY_FIELDS = ("user_reviews", "reviews_per_rating")


class ReviewPass:
    """Fills in reviews and rating histograms for leads that have a place id."""

    def __init__(
        self,
        *,
        browser: BrowserSession,
        settings: JobSettings,
        deadline: float,
        stop: threading.Event,
        proxies: list[str],
        progress: JobProgress,
        notes: list[str],
    ):
        """Use ``proxies`` in order, the first one for the whole pass."""
        self._browser = browser
        self._settings = settings
        self._deadline = deadline
        self._stop = stop
        self._proxies = proxies
        self._progress = progress
        self._notes = notes

    def run(self, rows: list[LeadRow]) -> int:
        """Read the panels of the leads still missing reviews; return how many got reviews."""
        todo = [r for r in rows if r.get("place_id")]
        if not todo:
            return 0
        proxy = self._proxies[0] if self._proxies else None
        reviewed = 0
        try:
            with self._browser.open_tab(proxy) as proxy_tab:
                tab = proxy_tab.tab
                if tab is None:
                    raise CDPError("browser returned no active tab")
                for name, value in CONSENT_COOKIES.items():
                    tab.set_secure_cookie(name, value, ".google.com")
                for position, row in enumerate(todo):
                    if self._stop.is_set():
                        break
                    if time.monotonic() >= self._deadline:
                        self._notes.append(
                            f"reviews read for {position} of {len(todo)} leads: ran out of time"
                        )
                        break
                    body = fetch_lead_panel_json(tab, self._panel_url(row))
                    if "/sorry/" in (tab.get_url() or ""):
                        self._notes.append(
                            f"reviews read for {position} of {len(todo)} leads: "
                            "Google's unusual-traffic page"
                        )
                        break
                    self._merge_panel(row, parse_place_panel(body or ""))
                    reviewed += 1
                    self._progress.save(
                        reviews_done=position + 1, reviews_total=len(todo)
                    )
        except (CDPError, OSError) as error:
            self._notes.append(f"the reviews pass stopped early: {error}")
        return reviewed

    def _panel_url(self, row: LeadRow) -> str:
        url = row["link"] + f"&hl={self._settings.language}"
        if self._settings.country:
            url += f"&gl={self._settings.country}"
        return url

    @staticmethod
    def _merge_panel(row: LeadRow, panel: PanelRecord) -> None:
        row["user_reviews"] = panel.get("user_reviews", "")
        row["reviews_per_rating"] = panel.get("reviews_per_rating", "")
        for key, value in panel.items():
            if is_lead_key(key) and key not in _PANEL_ONLY_FIELDS and not row.get(key):
                row[key] = str(value)
