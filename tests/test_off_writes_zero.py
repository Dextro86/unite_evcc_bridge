"""Tests for "uit = 0 A op de draad" in the EVCC bridge.

The bridge writes the charge current only on evcc commands, at session start
and on reconnect. The rules under test:

* A session start WITHOUT a known evcc intent (current_intent AND enabled_intent
  both None) writes 0 A - never resume_current (6 A).
* An explicit stop/disable always writes 0 A, even while a recovery pause is
  buffering positive currents.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import custom_components.unite_evcc_bridge.coordinator as coordinator_module
from custom_components.unite_evcc_bridge.coordinator import WebastoEvccCoordinator
from custom_components.unite_evcc_bridge.models import ChargerSnapshot


class FakeClient:
    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []
        self.stats = SimpleNamespace(connected=True, connection_epoch=0)
        self.snapshot: ChargerSnapshot | None = None

    async def write(self, register, value) -> None:
        self.writes.append((register.name, value))

    async def read_snapshot(self) -> ChargerSnapshot:
        assert self.snapshot is not None
        return self.snapshot

    async def read_optional_string_once(self, register):
        return None


def _coordinator(client: FakeClient) -> WebastoEvccCoordinator:
    hass = SimpleNamespace()
    entry = SimpleNamespace(entry_id="test", options={}, data={"host": "192.0.2.1"})
    return WebastoEvccCoordinator(
        hass,
        entry=entry,
        client=client,
        poll_interval=10,
        max_current=16,
        failsafe_current=6,
        failsafe_timeout=30,
        phase_recovery_enabled=False,
        phase_recovery_observe=60,
        phase_recovery_dwell=121,
    )


def test_reassert_current_without_evcc_intent_writes_zero() -> None:
    client = FakeClient()
    coord = _coordinator(client)
    coord.current_intent = None
    coord.enabled_intent = None

    asyncio.run(coord.async_reassert_current("a new session", refresh=False))

    # 0 A, not resume_current (6 A): evcc has not asked for anything yet.
    assert client.writes == [("current_limit", 0)]


def test_reassert_current_reasserts_evcc_intent() -> None:
    client = FakeClient()
    coord = _coordinator(client)
    coord.current_intent = 12
    coord.enabled_intent = True

    asyncio.run(coord.async_reassert_current("a new session", refresh=False))

    assert client.writes == [("current_limit", 12)]


def test_session_start_without_evcc_intent_writes_zero(monkeypatch) -> None:
    """End-to-end: a fresh session with no evcc intent writes 0 A, not 6 A.

    The wallbox applies its own hardware minimum for a new session; the bridge
    must not re-assert resume_current when evcc has never commanded anything.
    """
    client = FakeClient()
    coord = _coordinator(client)
    coord._vehicle_was_connected = False
    assert coord.current_intent is None
    assert coord.enabled_intent is None

    client.snapshot = ChargerSnapshot(available=True, cable_state=2, current_limit_a=6)

    async def _noop_ensure() -> None:
        return None

    async def _noop_heartbeat(client) -> None:
        return None

    monkeypatch.setattr(coord, "_async_ensure_connection_ownership", _noop_ensure)
    monkeypatch.setattr(coordinator_module, "write_heartbeat", _noop_heartbeat)

    asyncio.run(coord._async_update_data())

    assert client.writes == [("current_limit", 0)]


def test_explicit_stop_writes_zero_during_recovery_buffer() -> None:
    """evcc-stop always lands on the wire, even while a recovery pause buffers
    positive currents."""
    client = FakeClient()
    coord = _coordinator(client)
    coord._buffer_commands = True

    asyncio.run(coord.async_set_current(0))

    assert client.writes == [("current_limit", 0)]


def test_explicit_disable_writes_zero_during_recovery_buffer() -> None:
    """Switch-off always lands on the wire, even while a recovery pause buffers
    positive currents."""
    client = FakeClient()
    coord = _coordinator(client)
    coord._buffer_commands = True

    asyncio.run(coord.async_set_enabled(False))

    assert client.writes == [("current_limit", 0)]
