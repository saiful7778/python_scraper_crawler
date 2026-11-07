import pytest

from app.services.browser.cdp_client import CDPClient
from app.services.browser.cdp_error import CDPError
from app.services.browser.tab import BrowserTab
from tests.services.browser.fakes import FakeWebSocket, ScriptedBrowser, sequence

LOAD_EVENT = {"method": "Page.loadEventFired", "sessionId": "S1", "params": {}}


def _eval_reply(value):
    return {"result": {"result": {"value": value}}}


def _eval_error(text):
    return {"result": {"exceptionDetails": {"text": text}}}


def _requests(browser: ScriptedBrowser, method: str) -> list[dict]:
    return [request for request in browser.calls if request["method"] == method]


def test_init_creates_attaches_and_enables_domains(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)

    assert tab.target_id == "T1"
    assert tab.session_id == "S1"
    assert browser.methods() == [
        "Target.createTarget",
        "Target.attachToTarget",
        "Page.enable",
        "Runtime.enable",
        "Network.enable",
    ]
    assert browser.calls[0]["params"] == {"url": "about:blank"}
    assert browser.calls[1]["params"] == {"targetId": "T1", "flatten": True}
    assert all(request["sessionId"] == "S1" for request in browser.calls[2:])


def test_init_creates_target_in_given_browser_context(
    client: CDPClient, browser: ScriptedBrowser
):
    BrowserTab(client, context="ctx-1")

    assert browser.calls[0]["params"] == {
        "url": "about:blank",
        "browserContextId": "ctx-1",
    }


def test_init_navigates_to_the_given_url_after_enabling_domains(
    client: CDPClient, browser: ScriptedBrowser
):
    browser.events["Page.navigate"] = [LOAD_EVENT]

    BrowserTab(client, url="https://example.com")

    assert browser.methods()[-1] == "Page.navigate"
    assert browser.calls[-1]["params"] == {"url": "https://example.com"}


def test_init_applies_user_agent_before_navigating(
    client: CDPClient, browser: ScriptedBrowser
):
    client.user_agent = "TestAgent/1.0"
    browser.events["Page.navigate"] = [LOAD_EVENT]

    BrowserTab(client, url="https://example.com")

    methods = browser.methods()
    assert methods.index("Emulation.setUserAgentOverride") < methods.index(
        "Page.navigate"
    )
    override = _requests(browser, "Emulation.setUserAgentOverride")[0]
    assert override["params"] == {"userAgent": "TestAgent/1.0"}
    assert override["sessionId"] == "S1"


def test_init_skips_user_agent_when_not_configured(
    client: CDPClient, browser: ScriptedBrowser
):
    BrowserTab(client)

    assert "Emulation.setUserAgentOverride" not in browser.methods()


def test_init_failure_closes_the_target_and_reraises_original_error(
    client: CDPClient, browser: ScriptedBrowser
):
    browser.replies["Page.enable"] = {"error": {"message": "domain unavailable"}}

    with pytest.raises(CDPError, match="Page.enable: domain unavailable"):
        BrowserTab(client)

    assert _requests(browser, "Target.closeTarget")[0]["params"] == {"targetId": "T1"}


def test_init_failure_still_reraises_when_cleanup_also_fails(
    client: CDPClient, browser: ScriptedBrowser
):
    browser.replies["Page.enable"] = {"error": {"message": "domain unavailable"}}
    browser.replies["Target.closeTarget"] = {"error": {"message": "no such target"}}

    with pytest.raises(CDPError, match="Page.enable: domain unavailable"):
        BrowserTab(client)


def test_call_scopes_method_to_the_tab_session(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    browser.replies["DOM.getDocument"] = {"result": {"root": {"nodeId": 1}}}

    result = tab.call("DOM.getDocument", {"depth": 1})

    assert result == {"root": {"nodeId": 1}}
    request = _requests(browser, "DOM.getDocument")[0]
    assert request["sessionId"] == "S1"
    assert request["params"] == {"depth": 1}


def test_navigate_waits_for_the_load_event(client: CDPClient, browser: ScriptedBrowser):
    tab = BrowserTab(client)
    browser.events["Page.navigate"] = [LOAD_EVENT]

    tab.navigate("https://example.com/page")

    assert _requests(browser, "Page.navigate")[0]["params"] == {
        "url": "https://example.com/page"
    }


def test_navigate_raises_when_navigation_is_rejected(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    browser.replies["Page.navigate"] = {
        "result": {"errorText": "net::ERR_NAME_NOT_RESOLVED"}
    }

    with pytest.raises(
        CDPError, match="navigate https://nope.invalid: net::ERR_NAME_NOT_RESOLVED"
    ):
        tab.navigate("https://nope.invalid")


def test_navigate_times_out_without_load_event(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)

    with pytest.raises(CDPError, match="no Page.loadEventFired event"):
        tab.navigate("https://example.com", timeout=0.05)


def test_evaluate_returns_the_javascript_value(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    browser.replies["Runtime.evaluate"] = _eval_reply({"title": "Hi"})

    assert tab.evaluate("({title: 'Hi'})") == {"title": "Hi"}


def test_evaluate_requests_by_value_and_awaits_promises(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    browser.replies["Runtime.evaluate"] = _eval_reply(None)

    tab.evaluate("fetch('/x')")

    request = _requests(browser, "Runtime.evaluate")[0]
    assert request["sessionId"] == "S1"
    assert request["params"] == {
        "expression": "fetch('/x')",
        "returnByValue": True,
        "awaitPromise": True,
    }


def test_evaluate_returns_none_when_result_has_no_value(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    browser.replies["Runtime.evaluate"] = {"result": {"result": {"type": "undefined"}}}

    assert tab.evaluate("undefined") is None


def test_evaluate_raises_with_exception_description(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    browser.replies["Runtime.evaluate"] = {
        "result": {
            "exceptionDetails": {
                "text": "Uncaught",
                "exception": {"description": "ReferenceError: foo is not defined"},
            }
        }
    }

    with pytest.raises(
        CDPError, match="JavaScript: ReferenceError: foo is not defined"
    ):
        tab.evaluate("foo")


def test_evaluate_falls_back_to_exception_text(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    browser.replies["Runtime.evaluate"] = _eval_error("Uncaught SyntaxError")

    with pytest.raises(CDPError, match="JavaScript: Uncaught SyntaxError"):
        tab.evaluate("(")


def test_get_url_returns_location_href(client: CDPClient, browser: ScriptedBrowser):
    tab = BrowserTab(client)
    browser.replies["Runtime.evaluate"] = _eval_reply("https://example.com/")

    assert tab.get_url() == "https://example.com/"
    assert (
        _requests(browser, "Runtime.evaluate")[0]["params"]["expression"]
        == "location.href"
    )


def test_wait_for_truthy_returns_first_truthy_value(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    browser.replies["Runtime.evaluate"] = sequence(
        _eval_reply(False), _eval_reply(0), _eval_reply("ready")
    )

    assert tab.wait_for_truthy("document.readyState", timeout=5, interval=0) == "ready"


def test_wait_for_truthy_returns_none_on_timeout(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    browser.replies["Runtime.evaluate"] = _eval_reply(False)

    assert tab.wait_for_truthy("false", timeout=0.05, interval=0.01) is None


def test_wait_for_truthy_retries_errors_until_success(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    browser.replies["Runtime.evaluate"] = sequence(
        _eval_error("navigating"), _eval_error("navigating"), _eval_reply("done")
    )

    assert tab.wait_for_truthy("window.done", timeout=5, interval=0) == "done"


def test_wait_for_truthy_reraises_when_every_attempt_errored(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    browser.replies["Runtime.evaluate"] = _eval_error("boom")

    with pytest.raises(CDPError, match="JavaScript: boom"):
        tab.wait_for_truthy("broken()", timeout=0.05, interval=0.01)


def test_wait_for_truthy_returns_none_when_errors_follow_a_falsy_result(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    browser.replies["Runtime.evaluate"] = sequence(
        _eval_reply(False), _eval_error("boom")
    )

    assert tab.wait_for_truthy("maybe()", timeout=0.05, interval=0.01) is None


def test_set_secure_cookie_sets_a_secure_root_path_cookie(
    client: CDPClient, browser: ScriptedBrowser
):
    tab = BrowserTab(client)

    tab.set_secure_cookie("session", "abc", ".example.com")

    request = _requests(browser, "Network.setCookie")[0]
    assert request["sessionId"] == "S1"
    assert request["params"] == {
        "name": "session",
        "value": "abc",
        "domain": ".example.com",
        "path": "/",
        "secure": True,
    }


def test_close_closes_the_target(client: CDPClient, browser: ScriptedBrowser):
    tab = BrowserTab(client)

    tab.close()

    assert _requests(browser, "Target.closeTarget")[-1]["params"] == {"targetId": "T1"}


def test_close_ignores_errors_from_a_dead_connection(
    client: CDPClient, fake_ws: FakeWebSocket, browser: ScriptedBrowser
):
    tab = BrowserTab(client)
    fake_ws.send_error = OSError("broken pipe")

    tab.close()
