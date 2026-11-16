from app.helpers.http_fetcher.responses import header_value


def test_header_value_is_case_insensitive() -> None:
    headers = {"Content-Type": "text/html"}
    assert header_value(headers, "content-type") == "text/html"
    assert header_value(headers, "CONTENT-TYPE") == "text/html"


def test_header_value_is_empty_when_absent() -> None:
    assert header_value({"server": "x"}, "content-type") == ""
