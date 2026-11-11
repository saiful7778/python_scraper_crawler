import pytest


def test_max_pages_defaults_to_100():
    from cli.crawler_cli import build_parser

    args = build_parser().parse_args(["--website", "a.de"])

    assert args.max_pages == 100
    assert args.finish_incomplete is False


def test_max_pages_zero_is_rejected(capsys):
    from cli.crawler_cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--website", "a.de", "--max-pages", "0"])
    assert "--max-pages must be at least 1" in capsys.readouterr().err


def test_website_is_required():
    from cli.crawler_cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args([])
