import sys

import pytest

BASE_ARGS = ["--keywords", "coffee shops", "--city", "Austin", "--country", "us"]


def parse(monkeypatch: pytest.MonkeyPatch, *extra: str):
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(sys, "argv", ["google_map_scrape_cli.py", *BASE_ARGS, *extra])
    return read_query()


def _valid_query():
    from cli.google_map_scrape_cli import CliQuery

    return CliQuery.model_validate(
        {"keywords": ["coffee shops"], "city": "Austin", "country": "us"}
    )


def test_unsaved_leads_exit_with_the_message(monkeypatch, capsys):
    import cli.google_map_scrape_cli as cli
    from app.services.google_map_scraper.scraper import UnsavedLeadsError

    class Boom:
        def scrape(self, query, known):
            raise UnsavedLeadsError("s1", 4)

    monkeypatch.setattr(cli, "PlaceScraper", Boom)
    monkeypatch.setattr(cli, "read_query", lambda: _valid_query())
    monkeypatch.setattr(cli.asyncio, "run", lambda coro: (coro.close(), frozenset())[1])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert str(exit_info.value).startswith("✗ ")
    assert "4 lead(s)" in str(exit_info.value)


def test_keywords_are_split_on_commas(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.google_map_scrape_cli import read_query

    args = [
        "--keywords",
        "coffee shops,bakeries",
        "--city",
        "Austin",
        "--country",
        "us",
    ]
    monkeypatch.setattr(sys, "argv", ["google_map_scrape_cli.py", *args])
    assert read_query().keywords == ("coffee shops", "bakeries")


def test_keywords_drop_blanks_and_repeats(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.google_map_scrape_cli import keyword_names

    assert keyword_names(" coffee shops, bakeries ,,coffee shops") == [
        "coffee shops",
        "bakeries",
    ]
