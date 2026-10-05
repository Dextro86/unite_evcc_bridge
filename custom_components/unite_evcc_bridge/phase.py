"""Adaptive phase fixes: recovery (1->3) and downshift (3->1).

Mixin for WebastoEvccCoordinator: the observe-pause-resume machine both
directions share, plus the gates that decide when a stuck car needs one.
All state (latches, task, status) lives on the coordinator; this module only
holds the behaviour.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .models import ChargerSnapshot
from .registers import CURRENT_LIMIT, PHASE_SWITCH

if TYPE_CHECKING:
    from .coordinator import WebastoEvccCoordinator

_LOGGER = logging.getLogger(__name__)

_RECOVERY_IDLE = "idle"
_RECOVERY_OBSERVING = "observing_3p"
_RECOVERY_DWELLING = "dwelling"
_RECOVERY_RESUMING = "resuming"
_RECOVERY_COMPLETE = "complete"
_RECOVERY_ABORTED = "aborted"


class PhaseRecoveryMixin:
    """Observe-pause-resume machine, both phase directions."""

    # State below lives on the coordinator; declared here only so type
    # checkers see what the mixin expects from its host class.
    coordinator: WebastoEvccCoordinator
    hass: Any
    client: Any
    data: ChargerSnapshot | None
    phase_recovery_enabled: bool
    phase_recovery_observe: int
    phase_recovery_dwell: int
    requested_phase: str | None
    current_intent: int | None
    enabled_intent: bool | None
    resume_current: int
    recovery_status: str
    recovery_remaining_s: int
    last_recovery_at: Any
    last_recovery_result: str | None
    last_recovery_reason: str | None
    _recovery_task: Any
    _recovery_attempted: bool
    _downshift_attempted: bool
    _recovery_deadline: float | None
    _buffer_commands: bool
    _pending_current: int | None
    _pending_enabled: bool | None

    def _maybe_start_phase_recovery(self) -> None:
        if (
            not self.phase_recovery_enabled
            or self._recovery_attempted
            or self._recovery_task is not None
        ):
            return

        data = self.data
        if data is None or not data.available or not data.vehicle_connected:
            return
        if not data.charging_active:
            return
        if data.phase_mode != "1" and not self._measured_single_phase(data):
            return

        self._recovery_task = self.hass.async_create_task(self._run_phase_recovery())
        self.record_event("recovery_attempt", "phase recovery started")

    async def _guard_phase_setting(self, data: ChargerSnapshot) -> None:
        """Trede 1: keep register 405 converged with evcc's wish, mid-session.

        Only ever rewrites evcc's own wish (never invents one), only after 3
        consecutive polls of disagreement, and never while a recovery owns the
        charger. Worst case of a wrong call is one redundant identical write.
        """
        if self.recovery_active:
            self._guard_mismatches = 0
            return
        wish = self.requested_phase
        if wish not in {"1", "3"}:
            self._guard_mismatches = 0
            return
        if not data.vehicle_connected:
            self._guard_mismatches = 0
            return
        desired_raw = 0 if wish == "1" else 1
        try:
            actual_raw = int(await self.client.read(PHASE_SWITCH))
        except Exception as err:  # noqa: BLE001 - a failed read is not a mismatch
            _LOGGER.debug("Guardian could not read 405: %s", err)
            return
        if actual_raw == desired_raw:
            self._guard_mismatches = 0
            return
        self._guard_mismatches += 1
        if self._guard_mismatches < 3:
            return
        self._guard_mismatches = 0
        await self.client.write(PHASE_SWITCH, desired_raw)
        self.record_event(
            "405_write",
            f"guard requested={wish}P measured={actual_raw} written={desired_raw}",
        )

    def _maybe_start_phase_downshift(self) -> None:
        """Downshift mirror: 1P requested but the car measurably still on 3P."""
        if (
            not self.phase_recovery_enabled
            or self._downshift_attempted
            or self._recovery_task is not None
        ):
            return

        data = self.data
        if data is None or not data.available or not data.vehicle_connected:
            return
        if not data.charging_active:
            return
        if not self._measured_three_phase(data):
            return

        self._recovery_task = self.hass.async_create_task(
            self._run_phase_recovery(direction="down")
        )
        self.record_event("recovery_attempt", "phase downshift started")

    async def _run_phase_recovery(self, direction: str = "up") -> None:
        down = direction == "down"
        try:
            self._set_recovery_status(_RECOVERY_OBSERVING, self.phase_recovery_observe)
            await self._countdown(self.phase_recovery_observe)
            data = self.data
            if data is None or not data.available or not data.vehicle_connected:
                self._finish_recovery(_RECOVERY_ABORTED, "vehicle disconnected during observation")
                return
            if down:
                if not data.charging_active or self._measured_single_phase(data):
                    self._finish_recovery(_RECOVERY_COMPLETE, "measured 1p during observation")
                    return
            elif not data.charging_active or self._measured_three_phase(data):
                self._finish_recovery(_RECOVERY_COMPLETE, "measured 3p during observation")
                return

            if down:
                self._downshift_attempted = True
            else:
                self._recovery_attempted = True
            self._buffer_commands = True
            self._pending_current = self.current_intent or self.resume_current
            self._pending_enabled = self.enabled_intent if self.enabled_intent is not None else True

            _LOGGER.info(
                "%s phase recovery: live switch did not result in measured %s within %ss; "
                "holding charge current at 0A for %ss",
                "3->1" if down else "1->3",
                "1P" if down else "3P",
                self.phase_recovery_observe,
                self.phase_recovery_dwell,
            )
            async with self._command_lock:
                await self.client.write(CURRENT_LIMIT, 0)
            await self.async_request_refresh()

            self._set_recovery_status(_RECOVERY_DWELLING, self.phase_recovery_dwell)
            await self._countdown(self.phase_recovery_dwell)
            data = self.data
            if data is None or not data.available or not data.vehicle_connected:
                self._finish_recovery(_RECOVERY_ABORTED, "vehicle disconnected during dwell")
                return

            self._set_recovery_status(_RECOVERY_RESUMING, 0)
            current = self.current_intent if self.current_intent is not None else self._pending_current
            enabled = self.enabled_intent if self.enabled_intent is not None else self._pending_enabled
            if enabled is False:
                current = 0
            if current is None:
                current = self.resume_current
            async with self._command_lock:
                await self.client.write(CURRENT_LIMIT, current)
            _LOGGER.info(
                "%s phase recovery: resumed with %sA",
                "3->1" if down else "1->3",
                current,
            )
            self._finish_recovery(_RECOVERY_COMPLETE, "dwell completed")
            await self.async_request_refresh()
        except asyncio.CancelledError:
            if self.requested_phase == "1":
                self._set_recovery_status(_RECOVERY_IDLE, 0)
            else:
                self._finish_recovery(_RECOVERY_ABORTED, "cancelled")
            raise
        except Exception:
            _LOGGER.exception(
                "%s phase recovery failed", "3->1" if down else "1->3"
            )
            self._finish_recovery(_RECOVERY_ABORTED, "error")
        finally:
            self._buffer_commands = False
            self._pending_current = None
            self._pending_enabled = None
            self._recovery_deadline = None
            self._recovery_task = None

    async def _countdown(self, seconds: int) -> None:
        self._recovery_deadline = time.monotonic() + seconds
        while True:
            remaining = max(0, int(round(self._recovery_deadline - time.monotonic())))
            self.recovery_remaining_s = remaining
            self.async_update_listeners()
            if remaining <= 0:
                return
            await asyncio.sleep(min(1, remaining))

    def _set_recovery_status(self, status: str, remaining_s: int) -> None:
        self.recovery_status = status
        self.recovery_remaining_s = remaining_s
        self.async_update_listeners()

    def _finish_recovery(self, status: str, reason: str) -> None:
        self.last_recovery_at = dt_util.utcnow()
        self.last_recovery_result = status
        self.last_recovery_reason = reason
        self.record_event("recovery_done", f"{status}: {reason}")
        self._set_recovery_status(status, 0)
    def _cancel_recovery(self) -> None:
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            self._recovery_task = None
        self._buffer_commands = False
        self._pending_current = None
        self._pending_enabled = None
        self._set_recovery_status(_RECOVERY_IDLE, 0)

    @staticmethod
    def _measured_single_phase(data: ChargerSnapshot) -> bool:
        l1 = data.current_l1_a or 0.0
        l2 = data.current_l2_a or 0.0
        l3 = data.current_l3_a or 0.0
        return l1 >= 3.0 and l2 < 2.0 and l3 < 2.0

    @staticmethod
    def _measured_three_phase(data: ChargerSnapshot) -> bool:
        return (
            (data.current_l1_a or 0.0) >= 3.0
            and (data.current_l2_a or 0.0) >= 3.0
            and (data.current_l3_a or 0.0) >= 3.0
        )
