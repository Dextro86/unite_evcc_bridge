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
PHASE_CAPABILITY = Register("phase_capability", 404)

FAILSAFE_CURRENT = Register("failsafe_current", 2000, register_type=RegisterType.HOLDING)
FAILSAFE_TIMEOUT = Register("failsafe_timeout", 2002, register_type=RegisterType.HOLDING)
CURRENT_LIMIT = Register("current_limit", 5004, register_type=RegisterType.HOLDING)
PHASE_SWITCH = Register("phase_switch", 405, register_type=RegisterType.HOLDING)
ALIVE = Register("alive", 6000, register_type=RegisterType.HOLDING)
