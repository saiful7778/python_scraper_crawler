from dataclasses import dataclass

from fastapi_startkit.configuration import Config
from fastapi_startkit.environment import env


@dataclass
class CrawlerConfig(Config):
    max_workers: int = env("CRAWLER_MAX_WORKERS", 8)
