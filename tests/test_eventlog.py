"""Tests for the diagnostics event log, the version helper, and firmware read.

The ring buffer is pure observability (the coordinator never reads it back to
make decisions). Firmware decoding reuses the charger's tolerant string decoder
(ASCII or NUL-interleaved UTF-16) via the single-attempt setup read.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.unite_evcc_bridge.const import integration_version
from custom_components.unite_evcc_bridge.eventlog import MAX_EVENTS, EventLog
from custom_components.unite_evcc_bridge.modbus import WebastoBridgeClient
from custom_components.unite_evcc_bridge.registers import FIRMWARE_VERSION, decode_modbus_string


# --- ring buffer ------------------------------------------------------------
def test_eventlog_keeps_newest_at_cap() -> None:
    log = EventLog(maxlen=MAX_EVENTS)
    for i in range(MAX_EVENTS + 5):
        log.record("n", str(i))
    assert len(log) == MAX_EVENTS
    assert log.as_list()[0]["detail"] == "5"
    assert log.as_list()[-1]["detail"] == str(MAX_EVENTS + 4)


def test_eventlog_as_list_has_at_kind_detail() -> None:
    log = EventLog()
    log.record("405_write", "evcc 3P (reg=1)")
    entry = log.as_list()[0]
    assert set(entry) == {"at", "kind", "detail"}
    assert entry["kind"] == "405_write"
    assert entry["at"]  # ISO-8601 timestamp is populated


def test_eventlog_ordering_is_fifo() -> None:
    log = EventLog()
    log.record("a", "1")
    log.record("b", "2")
    assert [e["kind"] for e in log.as_list()] == ["a", "b"]


def test_eventlog_phase_events_survive_system_flood() -> None:
    log = EventLog()
    log.record("405_write", "evcc 1P (reg=0)")
    for i in range(40):
        log.record("reconnect", f"storm {i}")
    kinds = [e["kind"] for e in log.as_list()]
    assert "405_write" in kinds
    # System bucket keeps only its own newest.
    assert len(log) == 1 + MAX_EVENTS


# --- integration version (diagnostics content) ------------------------------
def test_integration_version_reads_manifest() -> None:
    assert integration_version() == "0.2.2-beta.1"


# --- firmware register + tolerant decoding ----------------------------------
def test_firmware_register_definition() -> None:
    assert FIRMWARE_VERSION.address == 230
    assert FIRMWARE_VERSION.count == 50


def _words_of(raw: bytes, count: int = 50) -> list[int]:
    raw = raw.ljust(count * 2, b"\x00")
    return [int.from_bytes(raw[i : i + 2], "big") for i in range(0, count * 2, 2)]


def test_firmware_decode_tolerant() -> None:
    assert decode_modbus_string(_words_of(b"3.187")) == "3.187"
    assert decode_modbus_string(_words_of("3.187".encode("utf-16-be"))) == "3.187"


def _client_returning(registers: list[int]) -> WebastoBridgeClient:
    async def read_input_registers(**kwargs):
        return SimpleNamespace(registers=registers, isError=lambda: False)

    client = WebastoBridgeClient("10.0.0.5", 502, 255)
    client._client = SimpleNamespace(
        connected=True,
        read_input_registers=read_input_registers,
        read_holding_registers=read_input_registers,
    )
    client._unit_kwarg = "slave"
    return client


def test_read_optional_string_once_decodes_firmware() -> None:
    client = _client_returning(_words_of(b"3.187"))
    assert asyncio.run(client.read_optional_string_once(FIRMWARE_VERSION)) == "3.187"


def test_read_optional_string_once_returns_none_on_refusal() -> None:
    async def boom(**kwargs):
        raise Exception("no such register")

    client = WebastoBridgeClient("10.0.0.5", 502, 255)
    client._client = SimpleNamespace(
        connected=True,
        read_input_registers=boom,
        read_holding_registers=boom,
    )
    client._unit_kwarg = "slave"
    assert asyncio.run(client.read_optional_string_once(FIRMWARE_VERSION)) is None
