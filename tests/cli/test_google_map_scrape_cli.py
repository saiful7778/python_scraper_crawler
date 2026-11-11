import sys

import pytest

BASE_ARGS = ["--keyword", "coffee shops", "--city", "Austin", "--country", "us"]


def parse(monkeypatch: pytest.MonkeyPatch, *extra: str):
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(sys, "argv", ["google_map_scrape_cli.py", *BASE_ARGS, *extra])
    return read_query()


def test_crawl_runs_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse(monkeypatch).no_crawl is False


def test_no_crawl_flag_skips_the_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse(monkeypatch, "--no-crawl").no_crawl is True
