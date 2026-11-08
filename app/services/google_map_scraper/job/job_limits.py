"""Tunable limits of the job engine. Durations are in seconds."""

from typing import Final

#: Waits before retrying a keyword that Google rate-limited, one per retry.
BACKOFF_SECONDS: Final[tuple[int, ...]] = (30, 60, 120)
#: Wait before retrying through the next proxy when several proxies are set.
ROTATE_WAIT_SECONDS: Final[int] = 5
#: Times a crashed browser is started again for the same job.
MAX_BROWSER_RELAUNCHES: Final[int] = 2
#: Extra time after ``max_time`` before the watchdog kills browser.
WATCHDOG_GRACE_SECONDS: Final[float] = 30
#: Time that must be left before a keyword with no result list is searched again.
NO_FEED_RETRY_MARGIN_SECONDS: Final[float] = 30
#: The website pass always gets at least this long, even after max_time ran out.
WEBSITE_MIN_SECONDS: Final[float] = 5
#: Concurrent plain-HTTP website requests.
WEBSITE_WORKERS: Final[int] = 8
#: Concurrent website renders (each one is a browser page).
RENDER_WORKERS: Final[int] = 2
#: Time the browser-rendering retry pass may take, at most.
RENDER_MAX_SECONDS: Final[float] = 30
