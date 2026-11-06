import pytest

from app.services.browser import cdp_client
from app.services.browser.cdp_client import CDPClient
from tests.services.browser.fakes import FakeWebSocket, ScriptedBrowser


@pytest.fixture
def fake_ws(monkeypatch: pytest.MonkeyPatch) -> FakeWebSocket:
    fake = FakeWebSocket()
    monkeypatch.setattr(
        cdp_client, "WebSocketClient", lambda url, timeout=30.0: fake
    )
    return fake


@pytest.fixture
def client(fake_ws: FakeWebSocket):
    cdp = CDPClient("ws://fake/devtools/browser", timeout=1.0)
    yield cdp
    cdp.close()


@pytest.fixture
def browser(fake_ws: FakeWebSocket) -> ScriptedBrowser:
    return ScriptedBrowser(fake_ws)
