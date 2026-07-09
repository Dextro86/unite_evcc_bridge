import asyncio

import aiohttp
import pytest

from custom_components.unite_evcc_bridge.control import derive_state, phase_mismatch
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


def test_phase_mismatch_detects_3p_configured_but_measured_1p() -> None:
    data = ChargerSnapshot(
        available=True,
        charging_state=1,
        phase_mode_raw=1,
        current_l1_a=15.0,
        current_l2_a=0.2,
        current_l3_a=0.3,
    )
    assert phase_mismatch(data) is True


def test_phase_mismatch_avoids_transient_or_non_charging_false_positives() -> None:
    assert phase_mismatch(
        ChargerSnapshot(
            available=True,
            charging_state=0,
            phase_mode_raw=1,
            current_l1_a=15.0,
            current_l2_a=0.2,
            current_l3_a=0.3,
        )
    ) is False
    assert phase_mismatch(
        ChargerSnapshot(
            available=True,
            charging_state=1,
            phase_mode_raw=1,
            current_l1_a=15.0,
            current_l2_a=3.1,
            current_l3_a=3.2,
        )
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
    assert called == ["http://10.0.0.5"]  # webconfig soft-reset actually fired
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
