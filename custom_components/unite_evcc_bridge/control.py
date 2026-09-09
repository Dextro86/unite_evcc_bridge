from __future__ import annotations

from dataclasses import dataclass

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


@dataclass
class RfidProbe:
    """Remembers whether the charger serves the optional RFID registers.

    The session-RFID tag (1516-1530) only exists on newer firmware. Probing it
    on every poll would hammer old wallboxes, so the client asks once per
    connection and this object remembers the answer:

    - clean refusal (the charger answers "no such registers") -> unsupported
      until the next reconnect; a single failed read per connection at most.
    - transport failure (timeout / connection lost mid-probe) while the
      mandatory reads are healthy -> the register is dangerous on this
      firmware; after ``max_transport_strikes`` it stays disabled for the rest
      of the Home Assistant session, so a crashy firmware cannot be kept in a
      probe -> crash -> reconnect loop. A reload probes again.
    - success resets the strike counter; new firmware never notices this.
    """

    supported: bool | None = None  # None = unknown, probe once
    transport_strikes: int = 0
    session_disabled: bool = False
    max_transport_strikes: int = 2

    @property
    def want_probe(self) -> bool:
        """Whether the client should attempt the RFID read this cycle."""
        return not self.session_disabled and self.supported is not False

    def reset_on_reconnect(self) -> None:
        """A new TCP connection may serve new firmware; probe again.

        Strike history and the session latch survive on purpose: they describe
        this wallbox, not this socket.
        """
        self.supported = None

    def note_ok(self) -> None:
        self.supported = True
        self.transport_strikes = 0

    def note_unsupported(self) -> None:
        self.supported = False

    def note_transport_error(self) -> bool:
        """Record a timeout/connection failure during the probe.

        Returns True when probing must stop for the rest of the session.
        """
        self.transport_strikes += 1
        if self.transport_strikes >= self.max_transport_strikes:
            self.session_disabled = True
            return True
        self.supported = False  # quiet until the next reconnect
        return False
