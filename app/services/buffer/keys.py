def state_key(scrape_id: str) -> str:
    return f"scrape:{scrape_id}:state"


def rows_key(scrape_id: str) -> str:
    return f"scrape:{scrape_id}:rows"


def known_key(scrape_id: str) -> str:
    return f"scrape:{scrape_id}:known"


def queue_key(scrape_id: str) -> str:
    return f"scrape:{scrape_id}:queue"


def processing_key(scrape_id: str) -> str:
    return f"scrape:{scrape_id}:processing"


def queued_key(scrape_id: str) -> str:
    return f"scrape:{scrape_id}:queued"


def active_key() -> str:
    return "buffer:active"


def crawl_key(website_id: str) -> str:
    return f"buffer:crawl:{website_id}"


def crawl_runs_key(website_id: str) -> str:
    return f"buffer:crawl:{website_id}:runs"


def crawl_pages_key(website_id: str) -> str:
    return f"buffer:crawl:{website_id}:pages"


def crawl_links_key(website_id: str) -> str:
    return f"buffer:crawl:{website_id}:links"


def crawl_findings_key(website_id: str) -> str:
    return f"buffer:crawl:{website_id}:findings"


def crawl_events_key(website_id: str) -> str:
    return f"buffer:crawl:{website_id}:events"


def crawl_finished_key() -> str:
    return "buffer:crawl_finished"


def crawl_owners_key() -> str:
    return "buffer:crawl_owners"


def crawl_queue_key() -> str:
    return "buffer:crawl_queue"


def crawl_processing_key() -> str:
    return "buffer:crawl_processing"


def crawl_queued_key() -> str:
    return "buffer:crawl_queued"
