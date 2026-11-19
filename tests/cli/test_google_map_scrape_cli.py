import argparse
import sys

import pytest

BASE_ARGS = ["--keyword", "coffee shops", "--city", "Austin", "--country", "us"]


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


def test_repeated_keyword_flags_make_one_keyword_each(monkeypatch) -> None:
    from cli.google_map_scrape_cli import read_query

    args = [
        "--keyword",
        "coffee shops",
        "--keyword",
        "bakeries",
        "--city",
        "Austin",
        "--country",
        "us",
    ]
    monkeypatch.setattr(sys, "argv", ["google_map_scrape_cli.py", *args])
    assert read_query().keywords == ("coffee shops", "bakeries")


def test_the_keywords_flag_is_gone(monkeypatch) -> None:
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "google_map_scrape_cli.py",
            "--keywords",
            "a,b",
            "--city",
            "A",
            "--country",
            "us",
        ],
    )
    with pytest.raises(SystemExit):
        read_query()


def test_proxies_split_on_commas_and_drop_blanks_and_repeats():
    from cli.google_map_scrape_cli import proxy_names

    assert proxy_names(" http://a:1 , http://b:2,,http://a:1 ") == [
        "http://a:1",
        "http://b:2",
    ]


def test_empty_proxies_value_is_rejected():
    from cli.google_map_scrape_cli import proxy_names

    with pytest.raises(argparse.ArgumentTypeError):
        proxy_names(" , ")


def test_repeated_proxies_flags_accumulate_and_dedupe(monkeypatch):
    query = parse(
        monkeypatch,
        "--proxies",
        "http://a:1,http://b:2",
        "--proxies",
        "http://a:1,http://c:3",
    )
    assert query.proxies == ("http://a:1", "http://b:2", "http://c:3")


def test_dry_run_counts_comma_separated_proxies(monkeypatch, capsys):
    import cli.google_map_scrape_cli as cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "google_map_scrape_cli.py",
            *BASE_ARGS,
            "--proxies",
            "http://a:1,http://b:2",
            "--dry-run",
        ],
    )
    cli.main()
    assert "Proxies: 2" in capsys.readouterr().out


def test_deep_scan_is_rejected(monkeypatch) -> None:
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(
        sys, "argv", ["google_map_scrape_cli.py", *BASE_ARGS, "--deep-scan"]
    )
    with pytest.raises(SystemExit):
        read_query()


def test_each_keyword_takes_the_depth_and_grid_that_follow_it(monkeypatch):
    from cli.google_map_scrape_cli import read_query

    args = [
        "--keyword",
        "coffee shops",
        "--depth",
        "6",
        "--grid",
        "1",
        "--keyword",
        "pizza",
        "--depth",
        "3",
        "--grid",
        "2",
        "--city",
        "Austin",
        "--state",
        "TX",
        "--country",
        "US",
    ]
    monkeypatch.setattr(sys, "argv", ["google_map_scrape_cli.py", *args])
    query = read_query()
    assert [(p.keyword, p.depth, p.grid) for p in query.keyword_plans] == [
        ("coffee shops", 6, 1),
        ("pizza", 3, 2),
    ]
    assert query.keywords == ("coffee shops", "pizza")


def test_a_keyword_without_flags_gets_depth_five_and_grid_one(monkeypatch):
    query = parse(monkeypatch, "-k", "pizza")
    assert [(p.keyword, p.depth, p.grid) for p in query.keyword_plans] == [
        ("coffee shops", 5, 1),
        ("pizza", 5, 1),
    ]


def test_a_numeric_keyword_is_a_name_not_a_depth(monkeypatch):
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(
        sys,
        "argv",
        ["google_map_scrape_cli.py", "-k", "3", "--city", "Austin", "--country", "us"],
    )
    assert [(p.keyword, p.depth) for p in read_query().keyword_plans] == [("3", 5)]


def test_a_depth_before_any_keyword_exits_with_a_named_error(monkeypatch):
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "google_map_scrape_cli.py",
            "--depth",
            "6",
            "-k",
            "pizza",
            "--city",
            "Austin",
            "--country",
            "us",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        read_query()
    assert "--depth must follow a --keyword flag" in str(exit_info.value)


def test_no_keyword_exits_with_the_required_message(monkeypatch):
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(
        sys, "argv", ["google_map_scrape_cli.py", "--city", "Austin", "--country", "us"]
    )
    with pytest.raises(SystemExit) as exit_info:
        read_query()
    assert "--keyword is required" in str(exit_info.value)


def test_a_depth_out_of_range_is_reported_against_the_depth_flag(monkeypatch):
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "google_map_scrape_cli.py",
            "-k",
            "pizza",
            "--depth",
            "11",
            "--city",
            "Austin",
            "--country",
            "us",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        read_query()
    assert "✗ --depth:" in str(exit_info.value)


def test_dry_run_prints_one_line_per_keyword(monkeypatch, capsys):
    import cli.google_map_scrape_cli as cli

    args = [
        "--keyword",
        "coffee shops",
        "--depth",
        "6",
        "--keyword",
        "pizza",
        "--depth",
        "3",
        "--grid",
        "2",
        "--city",
        "Austin",
        "--country",
        "us",
        "--dry-run",
    ]
    monkeypatch.setattr(sys, "argv", ["google_map_scrape_cli.py", *args])
    cli.main()
    out = capsys.readouterr().out
    assert "  'coffee shops': depth 6, 1 scrape(s), whole city" in out
    assert "  'pizza': depth 3, 4 scrape(s), 2x2 grid" in out
    assert "Scrapes: 5" in out
