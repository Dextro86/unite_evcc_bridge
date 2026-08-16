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


def is_three_phase_install(configured: str | None, reported_404: int | None) -> bool:
    """Whether the wallbox is wired for 3 phases.

    Register 404 alone is ambiguous: 0 means both "genuinely 1-phase installed"
    and "3-phase charger stuck at 1-phase". An explicit user setting wins; 404
    only provides the default before the user has answered.
    """
    if configured is not None:
        return configured == "3"
    return reported_404 != 0


def should_restore_phase_config(
    *,
    enabled: bool,
    rest_enabled: bool,
    vehicle_connected: bool,
    phase_capability_raw: int | None,
    grid_phases: str | None,
) -> bool:
    """Whether to re-apply the installation phase config after an unplug.

    Fired unconditionally after every unplug (the caller supplies the edge), not
    only when the charger looks stuck: register 405 is only reset to its default
    on a power cycle, reset or Modbus disconnect - never on unplug - so a session
    can start single-phase with every register reading correctly, which no 404
    check would catch. Toggling currentLimiterPhase re-applies the default.

    Only guard that survives: never on a genuine 1-phase install, where 404 = 0
    is correct and a 3-phase config must not be forced. It must also be opt-in,
    have a web-UI login, and the vehicle must be gone (the caller re-checks this
    just before the toggle, after the settle delay).
    """
    if not (enabled and rest_enabled):
        return False
    if vehicle_connected:
        return False
    return is_three_phase_install(grid_phases, phase_capability_raw)


def phase_mismatch(data: ChargerSnapshot, requested_3p: bool) -> bool:
    """True when 3-phase was actively requested but the car draws only 1.

    Gated on an explicit evcc 3-phase request, NOT on register 405: 405 rests at
    its 3-phase default on a 3-phase install, so a 1-phase car would otherwise
    look like a permanent mismatch.
    """
    if not requested_3p or data.charging_state != 1:
        return False
    return (
        (data.current_l1_a or 0.0) >= 3.0
        and (data.current_l2_a or 0.0) < 2.0
        and (data.current_l3_a or 0.0) < 2.0
    )
