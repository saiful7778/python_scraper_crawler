import re
from datetime import datetime, timedelta

from app.utils import utils
from app.utils.utils import log, utc_now


def test_utc_now_is_iso_utc_timestamp_to_the_second() -> None:
    value = utc_now()
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timedelta(0)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", value)


def test_log_writes_timestamped_message_to_stderr(capsys) -> None:
    log("engine started")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 engine started\n", captured.err
    )


def test_log_accepts_any_object(capsys) -> None:
    log(42)
    assert capsys.readouterr().err.endswith(" 42\n")


def test_log_ignores_closed_stream_errors(monkeypatch) -> None:
    def broken_print(*args, **kwargs):
        raise OSError("stderr closed")

    monkeypatch.setattr(utils, "print", broken_print, raising=False)
    log("lost message")
