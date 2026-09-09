"""Unit tests for baseline capture + restore (safety.py)."""
import asyncio

from custom_components.unite_evcc_bridge import safety as S
from custom_components.unite_evcc_bridge.modbus import ModbusError
from custom_components.unite_evcc_bridge.registers import (
    CURRENT_LIMIT,
    FAILSAFE_CURRENT,
    FAILSAFE_TIMEOUT,
    PHASE_SWITCH,
)


class FakeClient:
    """Minimal read/write stand-in using real Register objects."""

    def __init__(self, regs, fail_writes=None):
        self._regs = dict(regs)
        self.fail_writes = fail_writes or set()
        self.writes = []

    async def read(self, register):
        if register.address not in self._regs:
            raise ModbusError(f"no such register: {register.name}")
        return self._regs[register.address]

    async def write(self, register, value):
        if register.name in self.fail_writes:
            raise ModbusError(f"write failed: {register.name}")
        self.writes.append((register.name, value))
        self._regs[register.address] = value


FULL = {5004: 8, 2000: 12, 2002: 45, 405: 1}


def test_capture_reads_before_any_write():
    client = FakeClient(dict(FULL))
    baseline = asyncio.run(S.capture_baseline(client))
    assert client.writes == []
    assert baseline == {
        "current_limit": 8,
        "failsafe_current": 12,
        "failsafe_timeout": 45,
        "phase_switch": 1,
    }


def test_capture_skips_missing_registers():
    regs = {5004: 8, 2000: 12, 2002: 45}  # no 405 on this firmware
    baseline = asyncio.run(S.capture_baseline(FakeClient(regs)))
    assert baseline["phase_switch"] is None
    assert baseline["failsafe_current"] == 12


def test_restore_writes_back_and_verifies():
    client = FakeClient({5004: 0, 2000: 6, 2002: 30, 405: 0})
    baseline = {"current_limit": 8, "failsafe_current": 12,
                "failsafe_timeout": 45, "phase_switch": 1}
    assert asyncio.run(S.restore_baseline(client, baseline)) == []
    assert client._regs[5004] == 8
    assert client._regs[2000] == 12
    assert client._regs[2002] == 45
    assert client._regs[405] == 1


def test_restore_skips_none_and_reports_failures_but_continues():
    client = FakeClient(
        {5004: 0, 2000: 6, 2002: 30, 405: 0},
        fail_writes={"failsafe_current"},
    )
    baseline = {"current_limit": 8, "failsafe_current": 12,
                "failsafe_timeout": 45, "phase_switch": None}
    failed = asyncio.run(S.restore_baseline(client, baseline))
    assert failed == ["failsafe_current"]
    assert client._regs[5004] == 8
    assert client._regs[2002] == 45


def test_restore_reports_verify_mismatch():
    class LyingClient(FakeClient):
        async def read(self, register):
            if register.address == 2000:
                return 999
            return await super().read(register)

    client = LyingClient(dict(FULL))
    failed = asyncio.run(
        S.restore_baseline(client, {"current_limit": None, "failsafe_current": 12,
                                    "failsafe_timeout": None, "phase_switch": None})
    )
    assert failed == ["failsafe_current"]


def test_register_addresses_match_bridge_map():
    assert (CURRENT_LIMIT.address, FAILSAFE_CURRENT.address,
            FAILSAFE_TIMEOUT.address, PHASE_SWITCH.address) == (5004, 2000, 2002, 405)
