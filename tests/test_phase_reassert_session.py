"""Tests for phase re-assert at session start in the EVCC bridge.

At a new session the wallbox applies its own phase default, so after re-asserting
the charge current the bridge reads register 405 and writes the requested phase
back only when the measured value drifted (read-then-write, idempotent).

* drift -> one 405 write + a ``phase_reassert_session`` event
* no drift -> read only, no write
* no requested phase -> nothing (no read, no write)
* recovery active -> the whole session-start block is skipped
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import custom_components.unite_evcc_bridge.coordinator as coordinator_module
from custom_components.unite_evcc_bridge.coordinator import WebastoEvccCoordinator
from custom_components.unite_evcc_bridge.models import ChargerSnapshot
from custom_components.unite_evcc_bridge.registers import PHASE_SWITCH


class FakeClient:
    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []
        self.reads: list[str] = []
        self.phase_raw: int | None = None
        self.stats = SimpleNamespace(connected=True, connection_epoch=0)
        self.snapshot: ChargerSnapshot | None = None

    async def write(self, register, value) -> None:
        self.writes.append((register.name, value))

    async def read(self, register):
        self.reads.append(register.name)
        return self.phase_raw

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


def _run_session(
    client: FakeClient,
    coord: WebastoEvccCoordinator,
    monkeypatch,
    *,
    requested_phase: str | None,
    phase_raw: int | None,
    recovery_active: bool = False,
) -> None:
    coord._vehicle_was_connected = False
    coord.requested_phase = requested_phase
    client.phase_raw = phase_raw
    if recovery_active:
        coord._recovery_task = object()  # truthy -> recovery_active property True
    client.snapshot = ChargerSnapshot(available=True, cable_state=2, current_limit_a=6)

    async def _noop_ensure() -> None:
        return None

    async def _noop_heartbeat(client) -> None:
        return None

    monkeypatch.setattr(coord, "_async_ensure_connection_ownership", _noop_ensure)
    monkeypatch.setattr(coordinator_module, "write_heartbeat", _noop_heartbeat)

    asyncio.run(coord._async_update_data())


def test_session_start_writes_405_when_phase_drifted(monkeypatch) -> None:
    client = FakeClient()
    coord = _coordinator(client)

    _run_session(
        client, coord, monkeypatch, requested_phase="3", phase_raw=0  # drifted to 1P
    )

    assert ("phase_switch", 1) in client.writes
    events = [
        e for e in coord.event_log.as_list() if e["kind"] == "phase_reassert_session"
    ]
    assert events
    assert events[-1]["detail"] == "requested=3P measured=0 written=1"


def test_session_start_reads_405_but_writes_nothing_when_matching(monkeypatch) -> None:
    client = FakeClient()
    coord = _coordinator(client)

    _run_session(
        client, coord, monkeypatch, requested_phase="3", phase_raw=1  # already 3P
    )

    assert PHASE_SWITCH.name in client.reads          # 405 was read
    assert all(w[0] != "phase_switch" for w in client.writes)  # but not written


def test_session_start_without_requested_phase_does_nothing(monkeypatch) -> None:
    client = FakeClient()
    coord = _coordinator(client)

    _run_session(client, coord, monkeypatch, requested_phase=None, phase_raw=0)

    assert PHASE_SWITCH.name not in client.reads
    assert all(w[0] != "phase_switch" for w in client.writes)


def test_session_start_skips_phase_reassert_during_recovery(monkeypatch) -> None:
    client = FakeClient()
    coord = _coordinator(client)

    _run_session(
        client,
        coord,
        monkeypatch,
        requested_phase="3",
        phase_raw=0,
        recovery_active=True,
    )

    assert client.writes == []   # whole session-start block skipped
    assert client.reads == []
