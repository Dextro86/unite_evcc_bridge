from __future__ import annotations

from .models import ChargerSnapshot

STATE_IDLE = "idle"
STATE_CONNECTED = "connected"
STATE_CHARGING = "charging"
STATE_PHASE_MISMATCH = "phase_mismatch"
STATE_RECOVERY = "recovery"
STATE_RESTARTING = "restarting"
STATE_DISCONNECTED = "disconnected"
STATE_FAULT = "fault"

CHARGER_STATE_OPTIONS = [
    STATE_IDLE,
    STATE_CONNECTED,
    STATE_CHARGING,
    STATE_PHASE_MISMATCH,
    STATE_RECOVERY,
    STATE_RESTARTING,
    STATE_DISCONNECTED,
    STATE_FAULT,
]


def derive_state(
    *,
    connection_ok: bool,
    restarting: bool,
    faulted: bool,
    vehicle_connected: bool,
    charging: bool,
    phase_mismatch: bool,
    recovery_active: bool,
) -> str:
    if restarting:
        return STATE_RESTARTING
    if not connection_ok:
        return STATE_DISCONNECTED
    if faulted:
        return STATE_FAULT
    if recovery_active:
        return STATE_RECOVERY
    if vehicle_connected and charging:
        return STATE_PHASE_MISMATCH if phase_mismatch else STATE_CHARGING
    if vehicle_connected:
        return STATE_CONNECTED
    return STATE_IDLE


def phase_mismatch(data: ChargerSnapshot) -> bool:
    if data.charging_state != 1:
        return False

    configured_3p = data.phase_mode_raw == 1
    measured_1p = (
        (data.current_l1_a or 0.0) >= 3.0
        and (data.current_l2_a or 0.0) < 2.0
        and (data.current_l3_a or 0.0) < 2.0
    )
    return configured_3p and measured_1p
