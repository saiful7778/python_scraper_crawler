"""Named string values shared by the store, the worker and the public scrape view.

The values are plain strings (not enums) because they are persisted as JSON
and compared against rows written by older versions of the engine.
"""


class ScrapeStatus:
    """Lifecycle status of a scrape record."""

    PENDING = "pending"
    WORKING = "working"
    OK = "ok"
    FAILED = "failed"


class ScrapeStage:
    """Pipeline stage a scrape is in; persisted so an interrupted scrape can resume."""

    QUEUED = "queued"
    SEARCH = "search"
    REVIEWS = "reviews"
    WEBSITES = "websites"
    DONE = "done"


class SearchEndReason:
    """Interpretation of the reason string returned by the Maps collector."""

    BLOCKED = "blocked"
    NO_FEED = "no-feed"
    DOM_FALLBACK_SUFFIX = "+dom-fallback"
    TIMEOUT_PREFIX = "timeout"
    ERROR_PREFIX = "error: "

    @classmethod
    def is_blocked(cls, reason: str) -> bool:
        """Google answered with its unusual-traffic page."""
        return reason == cls.BLOCKED

    @classmethod
    def is_no_feed(cls, reason: str) -> bool:
        """Google showed no result list."""
        return reason == cls.NO_FEED

    @classmethod
    def used_dom_fallback(cls, reason: str) -> bool:
        """No result batch could be parsed, so only DOM data was kept."""
        return reason.endswith(cls.DOM_FALLBACK_SUFFIX)

    @classmethod
    def is_timeout(cls, reason: str) -> bool:
        """The search ran out of time (also true for ``timeout+dom-fallback``)."""
        return reason.startswith(cls.TIMEOUT_PREFIX)

    @classmethod
    def is_error(cls, reason: str) -> bool:
        """The collector reported an error."""
        return reason.startswith(cls.ERROR_PREFIX)

    @classmethod
    def error_message(cls, reason: str) -> str:
        """Return the error text of an error reason."""
        return reason[len(cls.ERROR_PREFIX) :]
