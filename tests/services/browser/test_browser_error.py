from app.services.browser.browser_error import BrowserError
from app.services.browser.cdp_error import CDPError


def test_browser_error_is_a_runtime_error():
    assert issubclass(BrowserError, RuntimeError)


def test_browser_error_is_distinct_from_cdp_error():
    assert not issubclass(BrowserError, CDPError)
    assert not issubclass(CDPError, BrowserError)
