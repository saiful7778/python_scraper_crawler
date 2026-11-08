"""Pure rules that turn what happened during a run into a final status and note."""

from .job_status import JobStatus


def describe_rate_limit(retries: int) -> str:
    """Explain that Google rate-limited this IP, mentioning retries if any."""
    return (
        "Google answered with its unusual-traffic page: this IP is rate-limited for now. "
        + (f"Waited and retried {retries} time(s). " if retries else "")
        + "Wait, or add proxies"
    )


def summarize_outcome(
    *,
    has_rows: bool,
    errors: list[str],
    blocked: bool,
    timed_out: bool,
    notes: list[str],
    retries: int,
) -> tuple[str, str]:
    """Decide the final ``(status, note)`` of a finished job.

    A job with rows (or one that had nothing to report) is ``ok`` and its note
    lists what went wrong along the way.  A job without rows that hit errors or
    a block is ``failed``.
    """
    rate_limit = describe_rate_limit(retries)
    if has_rows or not (errors or blocked):
        headlines: list[str] = []
        if timed_out:
            headlines.append("stopped at max_time; the rows found so far are kept")
        if errors:
            headlines.append(f"{len(errors)} keyword(s) failed: {errors[-1]}")
        extra_notes = notes
        if blocked:
            headlines = [f"{rate_limit}. The rows found before that are kept"]
            extra_notes = []
        return JobStatus.OK, "; ".join(headlines + extra_notes)
    return JobStatus.FAILED, rate_limit if blocked else errors[-1]


def search_percent(
    keyword_index: int, batches: int, scroll_depth: int, total_keywords: int
) -> int:
    """Return overall search progress in percent.

    ``keyword_index`` keywords are complete; the current one counts
    ``batches / scroll_depth`` (capped at 1) of a keyword.
    """
    current = min(batches / scroll_depth, 1)
    return int(100 * (keyword_index + current) / total_keywords)
