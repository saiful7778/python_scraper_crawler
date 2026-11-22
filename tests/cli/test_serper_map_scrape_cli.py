from uuid import uuid4

import pytest

from cli.serper_map_scrape_cli import (
    ask_resume,
    build_parser,
    load_api_keys,
    print_plan,
    read_config,
)

BASE = ["--keyword", "plumbers", "--city", "Austin", "--state", "TX", "--country", "us"]


def test_the_flags_build_the_config():
    config = read_config(BASE + ["--depth", "2", "--grid", "3", "--parallel", "7"])
    assert config.keywords == ("plumbers",)
    assert (config.plans[0].depth, config.plans[0].grid, config.parallel) == (2, 3, 7)


def test_repeated_keyword_flags_make_one_keyword_each():
    config = read_config(BASE + ["--keyword", "pizza"])
    assert config.keywords == ("plumbers", "pizza")


def test_each_keyword_keeps_the_depth_and_grid_that_follow_it():
    config = read_config(
        [
            "--keyword",
            "plumber",
            "--depth",
            "2",
            "--grid",
            "4",
            "--keyword",
            "hvac",
            "--depth",
            "4",
            "--grid",
            "2",
            "--city",
            "Austin",
            "--state",
            "TX",
            "--country",
            "us",
        ]
    )
    assert [(plan.keyword, plan.depth, plan.grid) for plan in config.plans] == [
        ("plumber", 2, 4),
        ("hvac", 4, 2),
    ]


def test_a_keyword_without_its_own_flags_gets_one_page_one_tile():
    config = read_config(BASE + ["--keyword", "pizza", "--depth", "4"])
    assert [(plan.depth, plan.grid) for plan in config.plans] == [(1, 1), (4, 1)]


def test_a_depth_before_any_keyword_is_refused():
    with pytest.raises(SystemExit) as info:
        read_config(["--depth", "2", *BASE])
    assert "--depth must follow a --keyword flag" in str(info.value)


def test_a_grid_before_any_keyword_is_refused():
    with pytest.raises(SystemExit) as info:
        read_config(["--grid", "3", *BASE])
    assert "--grid must follow a --keyword flag" in str(info.value)


def test_defaults_are_one_page_one_tile_and_five_parallel():
    config = read_config(BASE)
    assert (config.plans[0].depth, config.plans[0].grid, config.parallel) == (1, 1, 5)


def test_filters_default_off_and_can_be_turned_on():
    assert read_config(BASE).has_phone is False
    on = read_config(BASE + ["--has-phone", "--has-website"])
    assert on.has_phone is True and on.has_website is True


def test_a_bad_depth_exits_with_the_flag_named(capsys):
    with pytest.raises(SystemExit) as info:
        read_config(BASE + ["--depth", "0"])
    assert "--depth" in str(info.value)


def test_a_bad_parallel_exits_with_the_flag_named(capsys):
    with pytest.raises(SystemExit) as info:
        read_config(BASE + ["--parallel", "21"])
    assert "--parallel" in str(info.value)


def test_a_missing_keyword_exits():
    with pytest.raises(SystemExit) as info:
        read_config(["--city", "Austin", "--country", "us"])
    assert "--keyword" in str(info.value)


def test_a_missing_city_or_country_exits():
    with pytest.raises(SystemExit) as info:
        read_config(["--keyword", "plumbers", "--city", "Austin"])
    assert "--country" in str(info.value)


def test_the_parser_names_the_per_key_meaning_of_parallel():
    help_text = build_parser().format_help()
    assert "per API key" in help_text
    assert "page" in help_text.lower()
    # Both scoped flags say whose keyword they belong to (the help wraps, so
    # only the phrase's start is asserted as one piece).
    assert help_text.count("follows a") == 2


def test_the_keys_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEYS", " k1 , k2 ,, k1 ")
    assert load_api_keys() == ["k1", "k2"]


def test_a_missing_key_list_exits_with_the_variable_named(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEYS", raising=False)
    with pytest.raises(SystemExit) as info:
        load_api_keys()
    assert "SERPER_API_KEYS" in str(info.value)


def test_a_key_is_never_printed_in_the_plan(capsys):
    from app.repositories.serper_map_repository import SerperMapPage
    from app.services.serper.maps.serper_map_plan import SerperMapPlan

    config = read_config(BASE + ["--depth", "2"])
    plan = SerperMapPlan(
        config=config,
        center=(30.26, -97.74),
        bounding_box=None,
        pages=[
            SerperMapPage("plumbers", page, 0, 0, 30.26, -97.74, 15) for page in (1, 2)
        ],
    )
    print_plan(plan, ["secret-key-1"])
    printed = capsys.readouterr().out
    assert "secret-key-1" not in printed
    assert "2 page(s)" in printed
    assert "6 credits" in printed
    assert "1 key(s)" in printed


def test_the_plan_prints_each_keyword_with_its_own_effort(capsys):
    from app.repositories.serper_map_repository import SerperMapPage
    from app.services.serper.maps.serper_map_plan import SerperMapPlan

    config = read_config(
        [
            "--keyword",
            "plumber",
            "--depth",
            "2",
            "--grid",
            "4",
            "--keyword",
            "hvac",
            "--depth",
            "4",
            "--grid",
            "2",
            "--city",
            "Austin",
            "--state",
            "TX",
            "--country",
            "us",
        ]
    )
    pages = [
        SerperMapPage("plumber", 1, x, y, 30.26, -97.74, 12)
        for x in range(4)
        for y in range(4)
    ] + [
        SerperMapPage("hvac", 1, x, y, 30.26, -97.74, 13)
        for x in range(2)
        for y in range(2)
    ]
    print_plan(
        SerperMapPlan(
            config=config, center=(30.26, -97.74), bounding_box=None, pages=pages
        ),
        ["k"],
    )
    printed = capsys.readouterr().out
    assert "'plumber': depth 2, 16 tiles, 4x4 grid" in printed
    assert "'hvac': depth 4, 4 tiles, 2x2 grid" in printed
    assert "Tiles: 20" in printed
    assert "20 page(s)" in printed


def test_the_resume_question_accepts_yes_and_defaults_to_yes(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert ask_resume(_job_info()) is True
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert ask_resume(_job_info()) is False
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert ask_resume(_job_info()) is True


def test_the_resume_question_survives_end_of_input(monkeypatch):
    def eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert ask_resume(_job_info()) is False


def _job_info():
    from datetime import datetime
    from uuid import uuid4

    from app.repositories.serper_map_repository import SerperMapJobInfo

    return SerperMapJobInfo(
        id=uuid4(),
        name="plumbers @ Austin",
        status="exhausted",
        error="",
        config={},
        total_requests=18,
        done_requests=6,
        failed_requests=0,
        pending_requests=12,
        places_found=120,
        places_saved=100,
        credits_spent=18,
        created_at=datetime(2026, 11, 22, 10, 0),
        updated_at=datetime(2026, 11, 22, 10, 5),
    )


def _plan(config=None):
    from app.repositories.serper_map_repository import SerperMapPage
    from app.services.serper.maps import SerperMapConfig, SerperMapPlan

    config = config or SerperMapConfig(
        keywords=("plumbers",), city="Austin", state="TX", country="us"
    )
    return SerperMapPlan(
        config=config,
        center=(30.26, -97.74),
        bounding_box=None,
        pages=[SerperMapPage("plumbers", 1, 0, 0, 30.26, -97.74, 15)],
    )


def test_dry_run_writes_nothing_and_opens_no_database(monkeypatch, capsys):
    import cli.serper_map_scrape_cli as cli

    async def fake_plan(config):
        return _plan(config)

    monkeypatch.setattr(cli, "build_plan", fake_plan)
    monkeypatch.setenv("SERPER_API_KEYS", "k1")
    exit_code = cli.main([*BASE, "--dry-run"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "1 page(s)" in out and "3 credits" in out


def test_an_exhausted_run_exits_zero_and_says_the_job_is_saved(monkeypatch, capsys):
    import cli.serper_map_scrape_cli as cli
    from app.services.serper.maps import SerperMapResult

    class Exhausted:
        def __init__(self, *args, **kwargs):
            self._result = SerperMapResult(
                job_id=uuid4(),
                status="exhausted",
                requests_done=2,
                requests_failed=0,
                requests_pending=4,
                places_found=40,
                places_saved=30,
                credits_spent=6,
            )

        async def find_unfinished(self):
            return None

        async def start(self):
            return "j1"

        async def run(self, job_id):
            return self._result

        async def aclose(self):
            pass

    monkeypatch.setattr(cli, "SerperMapService", Exhausted)
    monkeypatch.setenv("SERPER_API_KEYS", "k1")
    exit_code = cli.main([*BASE, "--no-resume"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "credits" in out.lower() and "continue" in out.lower()


def test_a_failed_run_exits_one(monkeypatch, capsys):
    import cli.serper_map_scrape_cli as cli
    from app.services.serper.maps import SerperMapResult

    class Failed:
        def __init__(self, *args, **kwargs):
            pass

        async def find_unfinished(self):
            return None

        async def start(self):
            return "j1"

        async def run(self, job_id):
            return SerperMapResult(
                job_id=uuid4(),
                status="failed",
                requests_done=0,
                requests_failed=1,
                requests_pending=0,
                places_found=0,
                places_saved=0,
                credits_spent=0,
                error="every API key was refused",
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(cli, "SerperMapService", Failed)
    monkeypatch.setenv("SERPER_API_KEYS", "k1")
    assert cli.main([*BASE, "--no-resume"]) == 1


def test_the_unfinished_job_is_offered_and_resumed(monkeypatch, capsys):
    import cli.serper_map_scrape_cli as cli
    from app.services.serper.maps import SerperMapResult

    started, resumed = [], []
    job = _job_info()

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        async def find_unfinished(self):
            return job

        async def resume(self, job_id):
            resumed.append(job_id)
            return job_id

        async def start(self):
            started.append(True)
            return "new"

        async def run(self, job_id):
            return SerperMapResult(
                job_id=job_id,
                status="finished",
                requests_done=1,
                requests_failed=0,
                requests_pending=0,
                places_found=20,
                places_saved=18,
                credits_spent=3,
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(cli, "SerperMapService", Service)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    monkeypatch.setenv("SERPER_API_KEYS", "k1")
    assert cli.main(BASE) == 0
    assert resumed == [job.id]
    assert started == []


def test_a_missing_city_says_it_is_required():
    with pytest.raises(SystemExit) as info:
        read_config(["--keyword", "plumbers", "--country", "us"])
    assert "✗ --city is required." in str(info.value)


def test_an_empty_keyword_exits_with_the_flag_named():
    with pytest.raises(SystemExit) as info:
        read_config(["--city", "Austin", "--country", "us"])
    assert "--keyword" in str(info.value)
    assert "at least one keyword" in str(info.value)
