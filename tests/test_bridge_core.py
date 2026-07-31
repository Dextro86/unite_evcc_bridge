import asyncio

import aiohttp
import pytest

from custom_components.unite_evcc_bridge.control import (
    derive_state,
    is_three_phase_install,
    phase_mismatch,
)
from custom_components.unite_evcc_bridge.models import ChargerSnapshot, normalize_current_a
from custom_components.unite_evcc_bridge.modbus import (
    SESSION_BASE,
    SESSION_COUNT,
    TELEMETRY_BASE,
    TELEMETRY_COUNT,
    decode_u32,
)
from custom_components.unite_evcc_bridge.registers import ACTIVE_POWER, ENERGY_TOTAL, SESSION_DURATION, SESSION_ENERGY
from custom_components.unite_evcc_bridge.rest_client import (
    UnitePhpRestClient,
    UniteRestAuthError,
    UniteRestClient,
    UniteRestEndpointMissing,
    UniteRestError,
    async_restart_charger,
    async_restore_three_phase,
)


def test_decode_u32_big_endian_registers() -> None:
    assert decode_u32([0x0001, 0x0002]) == 65538


def test_block_ranges_cover_multi_word_registers() -> None:
    assert TELEMETRY_BASE <= ACTIVE_POWER.address
    assert ACTIVE_POWER.address + ACTIVE_POWER.count <= TELEMETRY_BASE + TELEMETRY_COUNT
    assert TELEMETRY_BASE <= ENERGY_TOTAL.address
    assert ENERGY_TOTAL.address + ENERGY_TOTAL.count <= TELEMETRY_BASE + TELEMETRY_COUNT
    assert SESSION_BASE <= SESSION_ENERGY.address
    assert SESSION_ENERGY.address + SESSION_ENERGY.count <= SESSION_BASE + SESSION_COUNT
    assert SESSION_BASE <= SESSION_DURATION.address
    assert SESSION_DURATION.address + SESSION_DURATION.count <= SESSION_BASE + SESSION_COUNT


def test_iec61851_status_mapping() -> None:
    assert ChargerSnapshot(available=True, cable_state=0, charging_state=0).iec61851_status == "A"
    assert ChargerSnapshot(available=True, cable_state=2, charging_state=0).iec61851_status == "B"
    assert ChargerSnapshot(available=True, cable_state=2, charging_state=1).iec61851_status == "C"


def test_phase_mode_mapping() -> None:
    assert ChargerSnapshot(available=True, phase_mode_raw=0).phase_mode == "1"
    assert ChargerSnapshot(available=True, phase_mode_raw=1).phase_mode == "3"
    assert ChargerSnapshot(available=True, phase_mode_raw=None).phase_mode is None


def test_evcc_float_current_is_rounded_and_clamped() -> None:
    assert normalize_current_a(0, 32) == 0
    assert normalize_current_a(4.2, 32) == 6
    assert normalize_current_a(6.82, 32) == 7
    assert normalize_current_a(40, 32) == 32


def test_derive_state_priority() -> None:
    base = {
        "connection_ok": True,
        "restarting": False,
        "faulted": False,
        "vehicle_connected": True,
        "charging": True,
        "phase_mismatch": False,
        "recovery_active": False,
    }
    assert derive_state(**{**base, "restarting": True, "connection_ok": False}) == "restarting"
    assert derive_state(**{**base, "connection_ok": False}) == "disconnected"
    assert derive_state(**{**base, "faulted": True}) == "fault"
    assert derive_state(**{**base, "recovery_active": True}) == "recovery"
    assert derive_state(**{**base, "phase_mismatch": True}) == "phase_mismatch"
    assert derive_state(**base) == "charging"
    assert derive_state(**{**base, "charging": False}) == "connected"
    assert derive_state(**{**base, "vehicle_connected": False, "charging": False}) == "idle"


def test_phase_mismatch_detects_3p_requested_but_measured_1p() -> None:
    data = ChargerSnapshot(
        available=True,
        charging_state=1,
        phase_mode_raw=1,
        current_l1_a=15.0,
        current_l2_a=0.2,
        current_l3_a=0.3,
    )
    assert phase_mismatch(data, requested_3p=True) is True


def test_phase_mismatch_ignores_1phase_car_without_3p_request() -> None:
    # The false positive we fixed: 405 rests at its 3-phase default, so a
    # 1-phase car draws only L1. Without an explicit 3-phase request that must
    # not be flagged.
    data = ChargerSnapshot(
        available=True,
        charging_state=1,
        phase_mode_raw=1,
        current_l1_a=15.0,
        current_l2_a=0.2,
        current_l3_a=0.3,
    )
    assert phase_mismatch(data, requested_3p=False) is False


def test_phase_mismatch_avoids_transient_or_non_charging_false_positives() -> None:
    assert phase_mismatch(
        ChargerSnapshot(
            available=True,
            charging_state=0,
            phase_mode_raw=1,
            current_l1_a=15.0,
            current_l2_a=0.2,
            current_l3_a=0.3,
        ),
        requested_3p=True,
    ) is False
    assert phase_mismatch(
        ChargerSnapshot(
            available=True,
            charging_state=1,
            phase_mode_raw=1,
            current_l1_a=15.0,
            current_l2_a=3.1,
            current_l3_a=3.2,
        ),
        requested_3p=True,
    ) is False


def test_rest_client_can_store_token_with_slots() -> None:
    session = object()
    client = UniteRestClient("192.0.2.1", "admin", "secret", session)
    assert client.session is session
    assert client._token is None
    client._token = "token"
    assert client._token == "token"


def test_rest_client_parses_json_with_wrong_content_type() -> None:
    class FakeResponse:
        status = 201
        content_type = "text/plain"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def text(self) -> str:
            return '{"access_token":"abc"}'

    class FakeSession:
        def request(self, *args, **kwargs):
            return FakeResponse()

    async def run() -> None:
        client = UniteRestClient("192.0.2.1", "admin", "secret", FakeSession())
        await client.test_connection()
        assert client._token == "abc"

    asyncio.run(run())


# --- restart orchestration + 404 fallback -----------------------------------
_LOGIN_FORM = '<input name="pass"><input name="button_login">'


class _Resp:
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


class _Raise:
    async def __aenter__(self):
        raise aiohttp.ClientError("port closed")

    async def __aexit__(self, *exc):
        return None


class RestartSession:
    """Drives async_restart_charger for the bridge client (uses .request())."""

    def __init__(self, json_ports, *, restart_status=404, login_status=201, webconfig_body=""):
        self.json_ports = set(json_ports)
        self.restart_status = restart_status
        self.login_status = login_status
        self.webconfig_body = webconfig_body
        self.restart_calls: list[str] = []

    def post(self, url, **kwargs):  # _probe_json_api (dummy-cred login probe)
        if any(f":{p}/" in url for p in self.json_ports):
            return _Resp(403)  # login endpoint exists, rejects probe creds
        return _Raise()

    def get(self, url, **kwargs):  # _has_webconfig
        return _Resp(200, self.webconfig_body)

    def request(self, method, url, **kwargs):  # UniteRestClient login + restart
        if url.endswith("/api/login"):
            body = '{"access_token":"tok"}' if self.login_status in (200, 201) else ""
            return _Resp(self.login_status, body)
        if "restart-system" in url:
            self.restart_calls.append(url)
            return _Resp(self.restart_status)
        return _Raise()


def test_restart_uses_json_when_endpoint_present() -> None:
    session = RestartSession({443}, restart_status=204)
    route = asyncio.run(async_restart_charger(session, "10.0.0.5", "admin", "x"))
    assert route == "json:443"


def test_restart_falls_back_to_webconfig_on_json_404(monkeypatch) -> None:
    called: list[str] = []

    async def fake_php_restart(self):
        called.append(self._base)

    monkeypatch.setattr(UnitePhpRestClient, "restart_system", fake_php_restart)
    # charger-2 pattern: JSON API on 4443, restart endpoint 404s, webconfig on 80
    session = RestartSession({4443}, restart_status=404, webconfig_body=_LOGIN_FORM)
    route = asyncio.run(async_restart_charger(session, "10.0.0.5", "admin", "x"))
    assert route == "webconfig"
    assert called == ["http://10.0.0.5"]  # webconfig reset actually fired
    assert session.restart_calls  # only after the JSON restart was attempted


def test_restart_auth_error_not_masked_by_fallback(monkeypatch) -> None:
    called: list[str] = []

    async def fake_php_restart(self):
        called.append(self._base)

    monkeypatch.setattr(UnitePhpRestClient, "restart_system", fake_php_restart)
    session = RestartSession({4443}, login_status=403, webconfig_body=_LOGIN_FORM)
    with pytest.raises(UniteRestAuthError):
        asyncio.run(async_restart_charger(session, "10.0.0.5", "admin", "x"))
    assert called == []  # bad credentials must not silently hit webconfig


def test_restart_json_404_and_no_webconfig_raises() -> None:
    session = RestartSession({4443}, restart_status=404, webconfig_body="<html>nope</html>")
    with pytest.raises(UniteRestError):
        asyncio.run(async_restart_charger(session, "10.0.0.5", "admin", "x"))


def test_json_restart_404_raises_endpoint_missing() -> None:
    session = RestartSession({443}, restart_status=404)
    client = UniteRestClient("10.0.0.5", "admin", "x", session, port=443)
    with pytest.raises(UniteRestEndpointMissing):
        asyncio.run(client.restart_system())


# --- phase-config restore (currentLimiterPhase 0->1) ------------------------
_PHASE_FIELD = "installationSettings.currentLimiterPhase"


class BridgeConfigSession:
    """JSON session: login ok, queued statuses for /configuration-updates.
    Records posted JSON bodies to assert the payload shape."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.bodies = []

    def request(self, method, url, **kwargs):
        if url.endswith("/api/login"):
            return _Resp(201, '{"access_token":"tok"}')
        self.bodies.append(kwargs.get("json"))
        return _Resp(self._statuses.pop(0))


def test_json_set_phase_accepts_plain_int() -> None:
    s = BridgeConfigSession([200])
    c = UniteRestClient("10.0.0.5", "admin", "x", s, port=443)
    asyncio.run(c.set_current_limiter_phase(0))
    assert s.bodies == [[{"fieldKey": _PHASE_FIELD, "value": 0}]]


def test_json_set_phase_falls_back_to_nested_on_422() -> None:
    s = BridgeConfigSession([422, 200])
    c = UniteRestClient("10.0.0.5", "admin", "x", s, port=443)
    asyncio.run(c.set_current_limiter_phase(1))
    assert s.bodies[0] == [{"fieldKey": _PHASE_FIELD, "value": 1}]
    assert s.bodies[1] == [
        {"fieldKey": _PHASE_FIELD, "value": {"value": 1, "valueType": "selection"}}
    ]


def test_php_selected_option_reads_current_limiter_value() -> None:
    html = (
        '<select name="currentLimiterValue">'
        '<option value="6">6</option>'
        '<option value="16" selected>16</option>'
        '</select>'
    )
    assert UnitePhpRestClient._selected_option(html, "currentLimiterValue") == "16"
    assert UnitePhpRestClient._selected_option(html, "nope") is None


class BridgeRestoreSession:
    def __init__(self, json_ports, *, config_status=200, webconfig_body=""):
        self.json_ports = set(json_ports)
        self.config_status = config_status
        self.webconfig_body = webconfig_body
        self.config_posts = []

    def post(self, url, **kwargs):  # _probe_json_api
        if any(f":{p}/" in url for p in self.json_ports):
            return _Resp(403)
        return _Raise()

    def get(self, url, **kwargs):  # _has_webconfig
        return _Resp(200, self.webconfig_body)

    def request(self, method, url, **kwargs):  # UniteRestClient login + config
        if url.endswith("/api/login"):
            return _Resp(201, '{"access_token":"tok"}')
        if "configuration-updates" in url:
            self.config_posts.append(kwargs.get("json"))
            return _Resp(self.config_status)
        return _Raise()


def test_restore_three_phase_toggles_via_json() -> None:
    session = BridgeRestoreSession({443})
    route = asyncio.run(
        async_restore_three_phase(session, "10.0.0.5", "admin", "x", settle_s=0)
    )
    assert route == "json:443"
    assert session.config_posts == [
        [{"fieldKey": _PHASE_FIELD, "value": 0}],
        [{"fieldKey": _PHASE_FIELD, "value": 1}],
    ]


def test_restore_three_phase_falls_back_to_webconfig(monkeypatch) -> None:
    calls = []

    async def fake_php_set(self, value):
        calls.append(value)

    monkeypatch.setattr(UnitePhpRestClient, "set_current_limiter_phase", fake_php_set)
    session = BridgeRestoreSession({4443}, config_status=404, webconfig_body=_LOGIN_FORM)
    route = asyncio.run(
        async_restore_three_phase(session, "10.0.0.5", "admin", "x", settle_s=0)
    )
    assert route == "webconfig"
    assert calls == [0, 1]


# --- 3-phase restore gating (1-phase installs must not get the button) ------
def test_is_three_phase_install() -> None:
    # explicit user setting wins in both directions
    assert is_three_phase_install("3", 0) is True   # stuck charger, user knows it is 3P
    assert is_three_phase_install("1", 1) is False  # user says 1P -> never offer restore
    # unset -> follow the charger's own register 404
    assert is_three_phase_install(None, 1) is True
    assert is_three_phase_install(None, 0) is False  # genuine 1-phase install
    assert is_three_phase_install(None, None) is True  # unknown -> assume 3P default
