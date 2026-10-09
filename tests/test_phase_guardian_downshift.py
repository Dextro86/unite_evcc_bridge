"""Tests for trede 1 (wish guardian) and trede 2 (3->1 downshift mirror).

Guardian: mid-session drift between evcc's wish and register 405 is rewritten
to the wish after 3 consecutive polls - never invents a wish, never disrupts.
Downshift: 1P requested but the car measurably still on 3P gets the same
observe-pause-resume sequence as the 1->3 recovery (opt-in, one per request).
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
        self.phase_raw: int | None = None
        self.stats = SimpleNamespace(connected=True, connection_epoch=0)
        self.snapshot: ChargerSnapshot | None = None

    async def write(self, register, value) -> None:
        self.writes.append((register.name, value))

    async def read(self, register):
        return self.phase_raw

    async def read_snapshot(self) -> ChargerSnapshot:
        assert self.snapshot is not None
        return self.snapshot

    async def read_optional_string_once(self, register):
        return None


class FakeHass:
    def async_create_task(self, coro):
        return asyncio.ensure_future(coro)


def _coordinator(client: FakeClient, **overrides) -> WebastoEvccCoordinator:
    hass = FakeHass()
    entry = SimpleNamespace(entry_id="test", options={}, data={"host": "192.0.2.1"})
    kwargs = {
        "poll_interval": 10,
        "max_current": 16,
        "failsafe_current": 6,
        "failsafe_timeout": 30,
        "phase_recovery_enabled": True,
        "phase_recovery_observe": 0,
        "phase_recovery_dwell": 0,
    }
    kwargs.update(overrides)
    return WebastoEvccCoordinator(hass, entry=entry, client=client, **kwargs)


def _charging_3p() -> ChargerSnapshot:
    return ChargerSnapshot(
        available=True,
        cable_state=2,      # connected
        charging_state=1,   # charging
        current_l1_a=15.0,
        current_l2_a=15.0,
        current_l3_a=15.0,
    )


def _update(coord: WebastoEvccCoordinator, monkeypatch, steady_session: bool = True) -> None:
    async def _noop_ensure() -> None:
        return None

    async def _noop_heartbeat(client) -> None:
        return None

    monkeypatch.setattr(coord, "_async_ensure_connection_ownership", _noop_ensure)
    monkeypatch.setattr(coordinator_module, "write_heartbeat", _noop_heartbeat)
    if steady_session:
        coord._vehicle_was_connected = True  # skip the session-start block
    asyncio.run(coord._async_update_data())


# --- trede 2: downshift -------------------------------------------------------
def test_downshift_full_sequence_pauses_and_resumes(monkeypatch) -> None:
    async def main():
        client = FakeClient()
        coord = _coordinator(client)
        coord.requested_phase = "1"
        coord.data = _charging_3p()
        coord._maybe_start_phase_downshift()
        assert coord._recovery_task is not None
        await coord._recovery_task  # observe(0) + dwell(0) run instantly
        return client.writes, coord._downshift_attempted, coord.last_recovery_result

    writes, attempted, result = asyncio.run(main())
    assert attempted is True
    assert ("current_limit", 0) in writes  # the re-negotiation pause
    assert writes[-1][0] == "current_limit" and writes[-1][1] > 0  # resumed
    assert result == "complete"


def test_downshift_needs_opt_in_and_measured_3p(monkeypatch) -> None:
    client = FakeClient()
    coord = _coordinator(client, phase_recovery_enabled=False)
    coord.requested_phase = "1"
    coord.data = _charging_3p()
    coord._maybe_start_phase_downshift()
    assert coord._recovery_task is None

    client2 = FakeClient()
    coord2 = _coordinator(client2)
    coord2.requested_phase = "1"
    calm = ChargerSnapshot(
        available=True,
        cable_state=2,
        charging_state=1,
        current_l1_a=15.0,
        current_l2_a=0.0,
        current_l3_a=0.0,
    )
    coord2.data = calm
    coord2._maybe_start_phase_downshift()
    assert coord2._recovery_task is None


def test_downshift_latch_one_per_1p_request() -> None:
    async def main():
        client = FakeClient()
        coord = _coordinator(client)
        coord.requested_phase = "1"
        coord.data = _charging_3p()
        coord._maybe_start_phase_downshift()
        await coord._recovery_task
        coord._maybe_start_phase_downshift()  # same wish again
        task_after = coord._recovery_task
        return task_after

    assert asyncio.run(main()) is None  # latch held: no new task


# --- trede 1: wish guardian ---------------------------------------------------
def test_guardian_rewrites_persistent_drift_to_wish(monkeypatch) -> None:
    client = FakeClient()
    coord = _coordinator(client, phase_recovery_enabled=False)
    coord.requested_phase = "1"
    client.phase_raw = 1  # 405 = 3P while wish is 1P
    client.snapshot = _charging_3p()

    _update(coord, monkeypatch)
    _update(coord, monkeypatch)
    assert ("phase_switch", 0) not in client.writes  # patience: 2 polls
    _update(coord, monkeypatch)
    assert ("phase_switch", 0) in client.writes      # 3rd consecutive poll writes
    assert ("phase_switch", 1) not in client.writes  # never the opposite


def test_guardian_quiet_when_converged_or_wish_unknown(monkeypatch) -> None:
    client = FakeClient()
    coord = _coordinator(client, phase_recovery_enabled=False)
    coord.requested_phase = "1"
    client.phase_raw = 0  # already 1P
    client.snapshot = _charging_3p()
    for _ in range(5):
        _update(coord, monkeypatch)
    assert client.writes == []

    coord.requested_phase = None
    client.phase_raw = 1
    for _ in range(5):
        _update(coord, monkeypatch)
    assert client.writes == []


# --- repairs: escalation counting + poll tracking ---------------------------
class _RepairHass(FakeHass):
    def __init__(self):
        self.tasks = []

    def async_create_task(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


def _repair_coordinator(client):
    hass = _RepairHass()
    entry = SimpleNamespace(entry_id="test", options={}, data={"host": "192.0.2.1"})
    coord = WebastoEvccCoordinator(
        hass,
        entry=entry,
        client=client,
        poll_interval=10,
        max_current=16,
        failsafe_current=6,
        failsafe_timeout=30,
        phase_recovery_enabled=True,
        phase_recovery_observe=0,
        phase_recovery_dwell=0,
    )
    return coord, hass


def test_two_escalations_schedule_repair():
    async def main():
        client = FakeClient()
        coord, hass = _repair_coordinator(client)
        coord.note_fix_escalated()
        assert coord._session_fix_failures == 1
        assert hass.tasks == []
        coord.note_fix_escalated()
        assert coord._session_fix_failures == 2
        for task in hass.tasks:
            await task  # repairs backend missing in tests: swallowed, must not crash
        coord.reset_fix_failures()
        assert coord._session_fix_failures == 0
        return len(hass.tasks)

    assert asyncio.run(main()) >= 1


def test_five_failed_polls_schedule_unreachable_repair():
    async def main():
        client = FakeClient()
        coord, hass = _repair_coordinator(client)
        for _ in range(4):
            coord._note_poll_failed()
        assert hass.tasks == []
        coord._note_poll_failed()
        for task in hass.tasks:
            await task
        coord._note_poll_ok()
        assert coord._failed_polls == 0
        return len(hass.tasks)

    assert asyncio.run(main()) >= 1
