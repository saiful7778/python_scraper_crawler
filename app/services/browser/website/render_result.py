"""The result every render returns, and the stages and statuses it can report."""

from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

#: ``"http"`` is set by callers that fetched the page without a browser; it is merged with
#: :func:`merge_render_status`.
RenderStatus = Literal[
    "rendered", "http", "blocked", "error", "timeout", "budget_exhausted"
]

RenderStage = Literal[
    "validate",
    "capacity",
    "browser_start",
    "navigation",
    "rendering",
    "read_dom",
    "complete",
]


class RenderResult(TypedDict):
    """The HTML of a rendered page, and why rendering ended with that status."""

    html: str
    status: RenderStatus
    stage: NotRequired[RenderStage]


def budget_exhausted() -> RenderResult:
    """Return the result for a render that had no time left to start."""
    return {"html": "", "status": "budget_exhausted"}


@dataclass
class RenderAttempt:
    """Tracks the stage a render reached, so a failure can report it."""

    stage: RenderStage = "validate"

    def failure(self, status: RenderStatus) -> RenderResult:
        return {"html": "", "status": status, "stage": self.stage}


def merge_render_status(current: RenderStatus, incoming: RenderStatus) -> RenderStatus:
    """Keep the more useful of two statuses for the same URL.

    A ``rendered`` page always wins, and an ``http`` result is kept over
    anything that is not ``rendered``.
    """
    if current == "rendered" or (current == "http" and incoming != "rendered"):
        return current
    return incoming
