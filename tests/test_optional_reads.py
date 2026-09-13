"""Unit tests for the single-attempt optional read path (_read_once)."""
import asyncio
from types import SimpleNamespace

from custom_components.unite_evcc_bridge.modbus import ModbusError, WebastoBridgeClient
from custom_components.unite_evcc_bridge.registers import PHASE_SWITCH


def _client_with_failing_transport():
    calls: list[str] = []

    async def boom(**kwargs):
        calls.append("read")
        raise ModbusError("no such register")

    client = WebastoBridgeClient("10.0.0.5", 502, 255)
    client._client = SimpleNamespace(
        connected=True,
        read_holding_registers=boom,
        read_input_registers=boom,
    )
    client._unit_kwarg = "slave"  # skip unit-kwarg probing
    return client, calls


def test_optional_int_returns_none_without_disconnect_or_retry():
    client, calls = _client_with_failing_transport()
    assert asyncio.run(client._optional_int(PHASE_SWITCH)) is None
    assert calls == ["read"]  # exactly one attempt, no retry storm
    assert client._client is not None  # connection kept
    assert client.stats.read_failures == 1


def test_optional_int_still_decodes_success():
    async def ok(**kwargs):
        return SimpleNamespace(registers=[1], isError=lambda: False)

    client = WebastoBridgeClient("10.0.0.5", 502, 255)
    client._client = SimpleNamespace(
        connected=True,
        read_holding_registers=ok,
        read_input_registers=ok,
    )
    client._unit_kwarg = "slave"
    assert asyncio.run(client._optional_int(PHASE_SWITCH)) == 1
