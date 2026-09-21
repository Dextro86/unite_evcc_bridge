from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RegisterType(StrEnum):
    INPUT = "input"
    HOLDING = "holding"


@dataclass(frozen=True, slots=True)
class Register:
    name: str
    address: int
    count: int = 1
    register_type: RegisterType = RegisterType.INPUT
    scale: float = 1.0


CHARGE_POINT_STATE = Register("charge_point_state", 1000)
CHARGING_STATE = Register("charging_state", 1001)
EQUIPMENT_STATE = Register("equipment_state", 1002)
CABLE_STATE = Register("cable_state", 1004)
CURRENT_L1 = Register("current_l1", 1008, scale=0.001)
CURRENT_L2 = Register("current_l2", 1010, scale=0.001)
CURRENT_L3 = Register("current_l3", 1012, scale=0.001)
VOLTAGE_L1 = Register("voltage_l1", 1014)
VOLTAGE_L2 = Register("voltage_l2", 1016)
VOLTAGE_L3 = Register("voltage_l3", 1018)
ACTIVE_POWER = Register("active_power", 1020, count=2)
ENERGY_TOTAL = Register("energy_total", 1036, count=2, scale=0.1)
SESSION_ENERGY = Register("session_energy", 1502, count=2, scale=0.001)
SESSION_DURATION = Register("session_duration", 1508, count=2)
# RFID tag of the active session (spec v1.9+, 2023). Read separately from the
# session block so a failure on older firmware never breaks session energy.
SESSION_RFID = Register("session_rfid", 1516, count=15)
# Charger identity for stable config-entry IDs (serial, never host:port).
SERIAL_NUMBER = Register("serial_number", 100, count=25)
PHASE_CAPABILITY = Register("phase_capability", 404)


def _decode_interleaved(core: bytes, encoding: str) -> str | None:
    """Decode NUL-interleaved bytes as UTF-16 when the pattern fits.

    Some firmware writes real RFID tags as UTF-16BE (NUL before every
    character) while the free-charging placeholder stays plain ASCII, so the
    encoding is detected per reading: every byte on the NUL side must be zero
    and every byte on the text side printable ASCII. Anything else returns
    None and the caller falls back to ASCII (the previous behaviour).
    """
    if len(core) % 2:
        return None
    text_bytes = core[1::2] if encoding == "utf-16-be" else core[0::2]
    nul_bytes = core[0::2] if encoding == "utf-16-be" else core[1::2]
    if any(b != 0 for b in nul_bytes):
        return None
    if not text_bytes or any(b < 0x20 or b > 0x7E for b in text_bytes):
        return None
    try:
        text = core.decode(encoding)
    except (UnicodeDecodeError, ValueError):
        return None
    text = text.strip(" ").strip()
    if not text or any(ord(c) < 0x20 or ord(c) > 0x7E for c in text):
        return None
    return text


def decode_modbus_string(registers: list[int]) -> str:
    """Decode string registers with firmware-tolerant encoding.

    Plain ASCII when the characters sit back-to-back (the free-charging
    placeholder), UTF-16 when NUL bytes interleave them (real RFID tags on
    some firmware). Read-only presentation: never touches the charger, and
    anything unrecognised falls back to the old ASCII decode.
    """
    data = bytearray()
    for word in registers:
        data.extend(int(word).to_bytes(2, "big"))
    core = bytes(data)
    while core.endswith(b"\x00\x00") and core:
        core = core[:-2]
    if not core.strip(b"\x00"):
        return ""
    for encoding in ("utf-16-be", "utf-16-le"):
        text = _decode_interleaved(core, encoding)
        if text is not None:
            return text
    return core.decode("ascii", errors="ignore").strip("\x00 ").strip()

FAILSAFE_CURRENT = Register("failsafe_current", 2000, register_type=RegisterType.HOLDING)
FAILSAFE_TIMEOUT = Register("failsafe_timeout", 2002, register_type=RegisterType.HOLDING)
CURRENT_LIMIT = Register("current_limit", 5004, register_type=RegisterType.HOLDING)
PHASE_SWITCH = Register("phase_switch", 405, register_type=RegisterType.HOLDING)
ALIVE = Register("alive", 6000, register_type=RegisterType.HOLDING)
