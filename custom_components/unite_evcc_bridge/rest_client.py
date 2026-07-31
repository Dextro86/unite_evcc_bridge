from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import re
from typing import Any

import aiohttp

from .const import REST_TIMEOUT_S

_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=8)
# CSRF token the legacy webconfig portal puts in every form.
_TOKEN_RE = re.compile(r'name="token"\s+value="([0-9a-fA-F]+)"')


class UniteRestError(Exception):
    """Raised when the charger web UI REST API fails."""


class UniteRestAuthError(UniteRestError):
    """Raised when web UI authentication fails."""


class UniteRestEndpointMissing(UniteRestError):
    """Raised when the JSON API lacks a requested endpoint (HTTP 404).

    Some firmware exposes a working JSON login on 443/4443 but does not serve
    the ``custom-actions`` restart endpoint. This lets the caller fall back to
    the legacy webconfig portal instead of failing outright.
    """


class UniteRestValidationError(UniteRestError):
    """A config write was rejected (HTTP 422). Used to retry with the other
    payload shape, which varies across firmware."""


# Installation phase-config field: JSON API key + webconfig select name.
# 0 = 1-phase, 1 = 3-phase.
_PHASE_FIELD = "installationSettings.currentLimiterPhase"


@dataclass(slots=True)
class UniteRestClient:
    host: str
    username: str
    password: str
    session: aiohttp.ClientSession
    timeout_s: int = REST_TIMEOUT_S
    port: int = 443
    _token: str | None = field(default=None, init=False, repr=False)

    @property
    def _base_url(self) -> str:
        return f"https://{self.host}:{self.port}/api"

    async def test_connection(self) -> None:
        self._token = None
        await self._login()

    async def restart_system(self) -> None:
        await self._request_with_auth("POST", "/custom-actions/restart-system")

    async def set_current_limiter_phase(self, value: int) -> None:
        """Set the installation phase config (0 = 1-phase, 1 = 3-phase).

        Payload shape varies across firmware: some accept a plain integer,
        others require a nested selection object. Try int, fall back on 422.
        """
        try:
            await self._request_with_auth(
                "POST", "/configuration-updates",
                json_body=[{"fieldKey": _PHASE_FIELD, "value": value}],
            )
        except UniteRestValidationError:
            await self._request_with_auth(
                "POST", "/configuration-updates",
                json_body=[{"fieldKey": _PHASE_FIELD, "value": {"value": value, "valueType": "selection"}}],
            )

    async def _request_with_auth(self, method: str, path: str, json_body: Any = None) -> Any:
        if self._token is None:
            await self._login()

        def _kwargs() -> dict[str, Any]:
            kw: dict[str, Any] = {"headers": {"Authorization": f"Bearer {self._token}"}}
            if json_body is not None:
                kw["json"] = json_body
            return kw

        response = await self._request(method, path, **_kwargs())
        if response["status"] == 401:
            self._token = None
            await self._login()
            response = await self._request(method, path, **_kwargs())

        if response["status"] == 404:
            raise UniteRestEndpointMissing(
                f"REST endpoint {path} not present on this firmware (HTTP 404)"
            )
        if response["status"] == 422:
            raise UniteRestValidationError(f"Config write to {path} rejected (HTTP 422)")
        if not 200 <= response["status"] < 300:
            raise UniteRestError(f"REST request {path} failed with HTTP {response['status']}")
        return response["body"]

    async def _login(self) -> None:
        response = await self._request(
            "POST",
            "/login",
            json={"username": self.username, "password": self.password},
        )
        if response["status"] in {401, 403}:
            raise UniteRestAuthError("Invalid web UI username or password")
        if response["status"] not in {200, 201}:
            raise UniteRestError(f"REST login failed with HTTP {response['status']}")

        body = response["body"]
        if not isinstance(body, dict):
            raise UniteRestError("REST login did not return JSON object")
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise UniteRestError("REST login response did not include access_token")
        self._token = token

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_s)
        url = f"{self._base_url}{path}"
        try:
            async with self.session.request(method, url, timeout=timeout, ssl=False, **kwargs) as response:
                body = await self._read_response_body(response)
                return {"status": response.status, "body": body}
        except asyncio.TimeoutError as err:
            raise UniteRestError(f"REST request timed out: {path}") from err
        except aiohttp.ClientError as err:
            raise UniteRestError(f"REST request failed: {err}") from err

    @staticmethod
    async def _read_response_body(response: aiohttp.ClientResponse) -> Any:
        text = await response.text()
        if not text:
            return ""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


class UnitePhpRestClient:
    """Legacy "webconfig" PHP portal (HTTP): session cookie + CSRF token.

    Uses its own short-lived session with an *unsafe* cookie jar, because the
    charger is an IP host and aiohttp's default jar drops cookies from IPs.
    """

    def __init__(self, host: str, username: str, password: str) -> None:
        self._base = f"http://{host}"
        self._username = username
        self._password = password

    @staticmethod
    def is_login_page(html: str) -> bool:
        return "button_login" in html

    @staticmethod
    def extract_token(html: str) -> str | None:
        m = _TOKEN_RE.search(html)
        return m.group(1) if m else None

    def _new_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT_S),
        )

    @staticmethod
    def _selected_option(html: str, name: str) -> str | None:
        """The selected <option value> of the <select name=...> in the page."""
        sm = re.search(
            r'<select\b[^>]*\bname=["\']?' + re.escape(name) + r'["\']?[^>]*>(.*?)</select>',
            html,
            re.S,
        )
        if not sm:
            return None
        om = re.search(
            r'<option[^>]*\bvalue=["\']?([^"\'>\s]*)["\']?[^>]*\bselected', sm.group(1)
        )
        return om.group(1) if om else None

    async def _login_and_page(self, session: aiohttp.ClientSession) -> str:
        """Log in and return the settings page HTML (holds the CSRF token + forms)."""
        try:
            async with session.get(f"{self._base}/") as r:
                await r.read()  # seed the PHPSESSID cookie
            form = {"username": self._username, "pass": self._password, "button_login": "Login"}
            async with session.post(f"{self._base}/", data=form, allow_redirects=False) as r:
                await r.read()
            async with session.get(f"{self._base}/") as r:
                html = await r.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UniteRestError(f"Cannot reach the charger web UI: {err}") from err
        if self.is_login_page(html):
            raise UniteRestAuthError("Invalid web UI username or password")
        return html

    async def _login_and_token(self, session: aiohttp.ClientSession) -> str:
        token = self.extract_token(await self._login_and_page(session))
        if not token:
            raise UniteRestError("Could not read the CSRF token after login")
        return token

    async def test_connection(self) -> None:
        async with self._new_session() as session:
            await self._login_and_token(session)

    async def restart_system(self) -> None:
        async with self._new_session() as session:
            token = await self._login_and_token(session)
            # Hard reset: restarts immediately regardless of state, matching the
            # JSON API's restart-system so the button behaves the same on every
            # firmware. Form POST to index_main.php with the CSRF token and the
            # submit button's default value "Submit Query" (an empty value is
            # ignored). The soft-reset variant ("button_soft_reset") was HAR-
            # verified; button_hard_reset is by analogy and still needs a live
            # check on a webconfig (Ethernet) charger.
            form = {"token": token, "button_hard_reset": "Submit Query"}
            try:
                async with session.post(f"{self._base}/index_main.php", data=form, allow_redirects=False) as r:
                    if r.status not in (200, 302, 303):
                        raise UniteRestError(f"Hard reset returned status {r.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise UniteRestError(f"Cannot reach the charger web UI: {err}") from err

    async def set_current_limiter_phase(self, value: int) -> None:
        """Set the installation phase config (0 = 1-phase, 1 = 3-phase) via the
        webconfig current-limiter form. Reads back the existing current-limiter
        value and re-sends it so only the phase changes."""
        async with self._new_session() as session:
            html = await self._login_and_page(session)
            token = self.extract_token(html)
            if not token:
                raise UniteRestError("Could not read the CSRF token after login")
            limiter_value = self._selected_option(html, "currentLimiterValue")
            if limiter_value is None:
                raise UniteRestError("Could not read currentLimiterValue from the web UI")
            form = {
                "token": token,
                "currentLimiterPhaseSelection": str(value),
                "currentLimiterValue": limiter_value,
                "button_current_limiter_settings": "Submit Query",
            }
            try:
                async with session.post(
                    f"{self._base}/index_main.php", data=form, allow_redirects=False
                ) as r:
                    if r.status not in (200, 302, 303):
                        raise UniteRestError(f"Phase config write returned status {r.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise UniteRestError(f"Cannot reach the charger web UI: {err}") from err


# The JSON API is served over HTTPS on 443 and/or 4443 (varies per firmware /
# interface); the legacy webconfig portal is on HTTP/80. A charger can expose
# both - we prefer the clean JSON API and fall back to webconfig.
JSON_API_PORTS = (443, 4443)
_API_STATUSES = {200, 201, 400, 401, 403}


async def _probe_json_api(session: aiohttp.ClientSession, host: str, port: int) -> bool:
    try:
        async with session.post(
            f"https://{host}:{port}/api/login",
            json={"username": "__probe__", "password": "__probe__"},
            ssl=False,
            timeout=_PROBE_TIMEOUT,
        ) as r:
            await r.read()
            return r.status in _API_STATUSES
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


async def _has_webconfig(session: aiohttp.ClientSession, host: str) -> bool:
    try:
        async with session.get(f"http://{host}/", timeout=_PROBE_TIMEOUT) as r:
            text = await r.text()
        return 'name="pass"' in text and "button_login" in text
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


async def async_build_rest_client(
    session: aiohttp.ClientSession, host: str, username: str, password: str
):
    """Detect which web UI the charger exposes and return the matching client."""
    for port in JSON_API_PORTS:
        if await _probe_json_api(session, host, port):
            return UniteRestClient(host, username, password, session, port=port)
    if await _has_webconfig(session, host):
        return UnitePhpRestClient(host, username, password)
    raise UniteRestError(
        "No reachable web UI found (tried the JSON API on 443/4443 and the HTTP webconfig portal)"
    )


async def async_restart_charger(
    session: aiohttp.ClientSession, host: str, username: str, password: str
) -> str:
    """Restart the charger over whichever web UI actually supports it.

    Prefers the clean JSON API, but some firmware serves a working JSON login
    without the restart endpoint (it 404s). In that case we fall back to the
    legacy webconfig reset. Returns a short label of the route used.
    Authentication failures are *not* swallowed -- they propagate so the caller
    can report ``auth_failed`` rather than masking bad credentials.
    """
    json_endpoint_missing = False
    for port in JSON_API_PORTS:
        if not await _probe_json_api(session, host, port):
            continue
        client = UniteRestClient(host, username, password, session, port=port)
        try:
            await client.restart_system()
            return f"json:{port}"
        except UniteRestEndpointMissing:
            json_endpoint_missing = True  # login works here but restart 404s

    if await _has_webconfig(session, host):
        await UnitePhpRestClient(host, username, password).restart_system()
        return "webconfig"

    if json_endpoint_missing:
        raise UniteRestError(
            "The JSON API has no restart endpoint on this firmware and no "
            "webconfig portal was found to fall back to"
        )
    raise UniteRestError(
        "No reachable web UI found (tried the JSON API on 443/4443 and the HTTP webconfig portal)"
    )


async def async_restore_three_phase(
    session: aiohttp.ClientSession,
    host: str,
    username: str,
    password: str,
    *,
    settle_s: float = 10.0,
) -> str:
    """Force the installation phase config back to 3-phase.

    Toggles ``currentLimiterPhase`` 0 -> (settle) -> 1 so a stuck desync (register
    404 = 0 while the UI still shows 3-phase) is re-synced; writing 1 alone can be
    a no-op when the config layer thinks it is already 3-phase. Uses the JSON
    config API where present, else the webconfig form. Returns the route used.
    Auth failures propagate.
    """
    json_endpoint_missing = False
    for port in JSON_API_PORTS:
        if not await _probe_json_api(session, host, port):
            continue
        client = UniteRestClient(host, username, password, session, port=port)
        try:
            await client.set_current_limiter_phase(0)
        except UniteRestEndpointMissing:
            json_endpoint_missing = True  # login works but no config endpoint here
            break
        await asyncio.sleep(settle_s)
        await client.set_current_limiter_phase(1)
        return f"json:{port}"

    if await _has_webconfig(session, host):
        php = UnitePhpRestClient(host, username, password)
        await php.set_current_limiter_phase(0)
        await asyncio.sleep(settle_s)
        await php.set_current_limiter_phase(1)
        return "webconfig"

    if json_endpoint_missing:
        raise UniteRestError(
            "The JSON API has no configuration endpoint on this firmware and no "
            "webconfig portal was found to fall back to"
        )
    raise UniteRestError(
        "No reachable web UI found (tried the JSON API on 443/4443 and the HTTP webconfig portal)"
    )
