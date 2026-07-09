from __future__ import annotations

from dataclasses import dataclass

from .const import DEFAULT_RESUME_CURRENT


def normalize_current_a(value: float, max_current: int) -> int:
    requested = int(round(value))
    if requested <= 0:
        return 0
    return max(DEFAULT_RESUME_CURRENT, min(max_current, requested))


@dataclass(frozen=True, slots=True)
class ChargerSnapshot:
    available: bool
    charge_point_state: int | None = None
    charging_state: int | None = None
    equipment_state: int | None = None
    cable_state: int | None = None
    current_limit_a: int | None = None
    phase_mode_raw: int | None = None
    phase_capability_raw: int | None = None
    active_power_w: float | None = None
    energy_total_kwh: float | None = None
    session_energy_kwh: float | None = None
    session_duration_s: int | None = None
    current_l1_a: float | None = None
    current_l2_a: float | None = None
    current_l3_a: float | None = None
    voltage_l1_v: float | None = None
    voltage_l2_v: float | None = None
    voltage_l3_v: float | None = None
    failsafe_current_a: int | None = None
    failsafe_timeout_s: int | None = None
    last_error: str | None = None

    @property
    def vehicle_connected(self) -> bool:
        return self.cable_state is not None and self.cable_state >= 2

    @property
    def charging_active(self) -> bool:
        if self.charging_state == 1:
            return True
        return max(self.current_l1_a or 0.0, self.current_l2_a or 0.0, self.current_l3_a or 0.0) >= 0.5

    @property
    def faulted(self) -> bool:
        return self.equipment_state == 2 or self.charge_point_state == 5

    @property
    def iec61851_status(self) -> str:
        if not self.vehicle_connected:
            return "A"
        if self.charging_active:
            return "C"
        return "B"

    @property
    def enabled(self) -> bool:
        return bool(self.current_limit_a and self.current_limit_a > 0)

    @property
    def phase_mode(self) -> str | None:
        if self.phase_mode_raw == 0:
            return "1"
        if self.phase_mode_raw == 1:
            return "3"
        return None
