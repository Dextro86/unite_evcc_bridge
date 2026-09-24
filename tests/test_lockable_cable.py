"""Unit tests for the lockable-cable webconfig methods."""
import asyncio

import aiohttp
import pytest

from custom_components.unite_evcc_bridge.rest_client import (
    UnitePhpRestClient,
    UniteRestAuthError,
    UniteRestError,
    _LOCKABLE_FIELD,
    async_set_lockable_cable,
)

_LOCKABLE_PAGE = (
    '<input type="hidden" name="token" value="abcdef123456">'
    '<select name="lockableCableSelection">'
    '<option value="0">Uitgeschakeld</option>'
    '<option value="1" selected="selected">Ingeschakeld</option>'
    "</select>"
)
_LOCKABLE_PAGE_OFF = (
    '<input type="hidden" name="token" value="abcdef123456">'
    '<select name="lockableCableSelection">'
    '<option value="0" selected="selected">Uitgeschakeld</option>'
    '<option value="1">Ingeschakeld</option>'
    "</select>"
)
_LOGIN_FORM = '<input name="pass"><input name="button_login">'


class _FakeResp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return b""


class _FakePhpSession:
    """GET returns the page, POST records the form."""

    def __init__(self, page_html: str, post_status: int = 302) -> None:
        self._page_html = page_html
        self._post_status = post_status
        self.posts: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def get(self, url, **kwargs):
        return _FakeResp(200, text=self._page_html)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("data", {})))
        return _FakeResp(self._post_status)


def _php_client(monkeypatch, page_html: str, post_status: int = 302):
    session = _FakePhpSession(page_html, post_status)
    monkeypatch.setattr(UnitePhpRestClient, "_new_session", lambda self: session)
    return UnitePhpRestClient("10.0.0.5", "admin", "secret"), session


def test_get_lockable_cable_on(monkeypatch) -> None:
    client, _ = _php_client(monkeypatch, _LOCKABLE_PAGE)
    assert asyncio.run(client.get_lockable_cable()) is True


def test_get_lockable_cable_off(monkeypatch) -> None:
    client, _ = _php_client(monkeypatch, _LOCKABLE_PAGE_OFF)
    assert asyncio.run(client.get_lockable_cable()) is False


def test_get_lockable_cable_absent(monkeypatch) -> None:
    client, _ = _php_client(monkeypatch, "<html>no such setting</html>")
    assert asyncio.run(client.get_lockable_cable()) is None


def test_set_lockable_cable_posts_form(monkeypatch) -> None:
    client, session = _php_client(monkeypatch, _LOCKABLE_PAGE_OFF)
    asyncio.run(client.set_lockable_cable(True))
    assert len(session.posts) == 2  # login POST + settings POST
    url, form = session.posts[1]
    assert url == "http://10.0.0.5/index_main.php"
    assert form["token"] == "abcdef123456"
    assert form["lockableCableSelection"] == "1"
    assert form["button_lockable_cable"] == "Submit Query"


def test_set_lockable_cable_bad_status_raises(monkeypatch) -> None:
    client, _ = _php_client(monkeypatch, _LOCKABLE_PAGE, post_status=500)
    with pytest.raises(UniteRestError):
        asyncio.run(client.set_lockable_cable(False))


def test_lockable_cable_bad_credentials_raise(monkeypatch) -> None:
    client, _ = _php_client(monkeypatch, _LOGIN_FORM)
    with pytest.raises(UniteRestAuthError):
        asyncio.run(client.get_lockable_cable())


class _ConnectionClosed(aiohttp.ClientError):
    """Fake closed port: caught like a real transport failure."""


class _RouteFakeResp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return b""


class _RouteSession:
    """Drives async_set_lockable_cable: JSON probe + config writes + webconfig."""

    def __init__(self, json_ports, *, config_status=200, webconfig_body="") -> None:
        self.json_ports = set(json_ports)
        self.config_status = config_status
        self.webconfig_body = webconfig_body
        self.config_posts: list = []

    def post(self, url, **kwargs):  # _probe_json_api
        if any(f":{p}/" in url for p in self.json_ports):
            return _RouteFakeResp(403)
        raise _ConnectionClosed("port closed")

    def get(self, url, **kwargs):  # _has_webconfig
        return _RouteFakeResp(200, self.webconfig_body)

    def request(self, method, url, **kwargs):  # UniteRestClient login + config
        if url.endswith("/api/login"):
            return _RouteFakeResp(201, '{"access_token":"tok"}')
        if "configuration-updates" in url:
            self.config_posts.append(kwargs.get("json"))
            return _RouteFakeResp(self.config_status)
        raise _ConnectionClosed("port closed")


def test_set_lockable_cable_prefers_json() -> None:
    session = _RouteSession({443})
    route = asyncio.run(
        async_set_lockable_cable(session, "10.0.0.5", "admin", "x", True)  # type: ignore[arg-type]
    )
    assert route == "json:443"
    assert session.config_posts == [[{"fieldKey": _LOCKABLE_FIELD, "value": 1}]]


def test_set_lockable_cable_falls_back_to_webconfig(monkeypatch) -> None:
    calls: list[bool] = []

    async def fake_php_set(self, enabled):
        calls.append(enabled)

    monkeypatch.setattr(UnitePhpRestClient, "set_lockable_cable", fake_php_set)
    session = _RouteSession({443}, config_status=404, webconfig_body=_LOGIN_FORM)
    route = asyncio.run(
        async_set_lockable_cable(session, "10.0.0.5", "admin", "x", False)  # type: ignore[arg-type]
    )
    assert route == "webconfig"
    assert calls == [False]


def test_set_lockable_cable_server_error_falls_back_to_webconfig(monkeypatch) -> None:
    calls: list[bool] = []

    async def fake_php_set(self, enabled):
        calls.append(enabled)

    monkeypatch.setattr(UnitePhpRestClient, "set_lockable_cable", fake_php_set)
    # JSON login works but the config endpoint 500s (twice: write + retry)
    # -> webconfig
    session = _RouteSession({443}, config_status=500, webconfig_body=_LOGIN_FORM)
    route = asyncio.run(
        async_set_lockable_cable(session, "10.0.0.5", "admin", "x", True)  # type: ignore[arg-type]
    )
    assert route == "webconfig"
    assert calls == [True]
    assert len(session.config_posts) == 2  # one write + one retry, then fallback
