from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time

from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from homeassistant.const import CONF_HOST
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .control import is_three_phase_install, phase_mismatch
from .const import (
    CONF_GRID_PHASES,
    CONF_PHASE_RESTORE_ON_UNPLUG,
    CONF_REST_ENABLED,
    CONF_REST_PASSWORD,
    CONF_REST_USERNAME,
    DEFAULT_PHASE_RESTORE_ON_UNPLUG,
    DEFAULT_REST_ENABLED,
    DEFAULT_REST_USERNAME,
    DEFAULT_RESUME_CURRENT,
    DOMAIN,
)
from .rest_client import UniteRestError, async_restore_three_phase
from .modbus import WebastoBridgeClient
from .models import ChargerSnapshot, normalize_current_a
from .registers import CURRENT_LIMIT, PHASE_SWITCH
from .safety import program_connection_ownership, write_heartbeat

_LOGGER = logging.getLogger(__name__)

_RECOVERY_IDLE = "idle"
_RECOVERY_OBSERVING = "observing_3p"
_RECOVERY_DWELLING = "dwelling"
_RECOVERY_RESUMING = "resuming"
_RECOVERY_COMPLETE = "complete"
_RECOVERY_ABORTED = "aborted"


def effective_poll_interval_s(poll_interval: int, failsafe_timeout: int) -> int:
    return min(poll_interval, max(3, failsafe_timeout // 2))


class WebastoEvccCoordinator(DataUpdateCoordinator[ChargerSnapshot]):
    def __init__(
        self,
        hass,
        *,
        entry,
        client: WebastoBridgeClient,
        poll_interval: int,
        max_current: int,
        failsafe_current: int,
        failsafe_timeout: int,
        phase_recovery_enabled: bool,
        phase_recovery_observe: int,
        phase_recovery_dwell: int,
    ) -> None:
        effective_interval = effective_poll_interval_s(poll_interval, failsafe_timeout)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=effective_interval),
        )
        self.entry = entry
        self.client = client
        self._vehicle_was_connected = False
        self._auto_restore_task: asyncio.Task | None = None
        self.configured_poll_interval = poll_interval
        self.effective_poll_interval = effective_interval
        self.max_current = max_current
        self.failsafe_current = failsafe_current
        self.failsafe_timeout = failsafe_timeout
        self.phase_recovery_enabled = phase_recovery_enabled
        self.phase_recovery_observe = phase_recovery_observe
        self.phase_recovery_dwell = phase_recovery_dwell
        self.resume_current = DEFAULT_RESUME_CURRENT
        self.requested_phase: str | None = None
        # requested_phase is seeded from the charger's resting phase (405 defaults
        # to 3P on a 3P install), so it is not proof that evcc asked for 3-phase.
        # Only an explicit select_option counts for the phase-mismatch check.
        self._phase_explicitly_requested: bool = False
        self.current_intent: int | None = None
        self.enabled_intent: bool | None = None
        self.recovery_status = _RECOVERY_IDLE
        self.recovery_remaining_s = 0
        self._initialized_connection_epoch = 0
        self._command_lock = asyncio.Lock()
        self._recovery_task: asyncio.Task[None] | None = None
        self._recovery_attempted = False
        self._recovery_deadline: float | None = None
        self._buffer_commands = False
        self._pending_current: int | None = None
        self._pending_enabled: bool | None = None
        self.rest_restart_until = 0.0
        self.last_recovery_at = None
        self.last_recovery_result: str | None = None
        self.last_recovery_reason: str | None = None
        self.last_rest_restart_at = None
        self.last_rest_restart_result: str | None = None
        self.last_rest_restart_reason: str | None = None

    async def _async_update_data(self) -> ChargerSnapshot:
        try:
            await self._async_ensure_connection_ownership()
        except Exception as err:  # noqa: BLE001
            return ChargerSnapshot(available=False, last_error=str(err))

        data = await self.client.read_snapshot()
        if not data.available:
            return data

        if self.requested_phase is None:
            self.requested_phase = data.phase_mode
        if self.current_intent is None:
            self.current_intent = data.current_limit_a
        if self.enabled_intent is None:
            self.enabled_intent = data.enabled
        if not data.vehicle_connected:
            self._recovery_attempted = False
        self._maybe_auto_restore_phase(data)

        try:
            await write_heartbeat(self.client)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Heartbeat failed: %s", err)
            return ChargerSnapshot(available=False, last_error=str(err))

        return data

    def _maybe_auto_restore_phase(self, data: ChargerSnapshot) -> None:
        """Re-sync a stuck 1-phase config at unplug, if the user opted in.

        The web-UI toggle that fixes this breaks the running charging session, so
        the only free moment is right after the vehicle is unplugged: the next
        plug-in then starts with the config already correct. Only fires when the
        charger really is stuck (register 404 reads 0) on an installation the
        user declared as 3-phase - on a genuine 1-phase wallbox 404 = 0 is
        correct and must be left alone.
        """
        connected = data.vehicle_connected
        just_unplugged = self._vehicle_was_connected and not connected
        self._vehicle_was_connected = connected
        if not just_unplugged:
            return
        o = self.entry.options
        if not o.get(CONF_PHASE_RESTORE_ON_UNPLUG, DEFAULT_PHASE_RESTORE_ON_UNPLUG):
            return
        if not o.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED):
            return
        if data.phase_capability_raw != 0:
            return  # not stuck
        if not is_three_phase_install(o.get(CONF_GRID_PHASES), data.phase_capability_raw):
            return  # genuinely 1-phase (or unanswered) -> never write a 3-phase config
        if self._auto_restore_task is not None and not self._auto_restore_task.done():
            return
        self._auto_restore_task = self.hass.async_create_task(self._async_auto_restore_phase())

    async def _async_auto_restore_phase(self) -> None:
        o = self.entry.options
        host = o.get(CONF_HOST, self.entry.data[CONF_HOST])
        try:
            route = await async_restore_three_phase(
                async_get_clientsession(self.hass),
                host,
                o.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME),
                o.get(CONF_REST_PASSWORD, ""),
            )
        except UniteRestError as err:
            _LOGGER.warning(
                "Charger is stuck on 1-phase, but the automatic restore failed: %s", err
            )
            return
        _LOGGER.info(
            "Vehicle unplugged with the charger stuck on 1-phase; restored the "
            "3-phase config via %s",
            route,
        )
        await self.async_request_refresh()

    async def _async_ensure_connection_ownership(self) -> None:
        if (
            self.client.stats.connected
            and self._initialized_connection_epoch == self.client.stats.connection_epoch
        ):
            return

        current = self.current_intent
        if self._buffer_commands or self.enabled_intent is False:
            current = 0
        if current is None:
            current = 0

        await program_connection_ownership(
            self.client,
            failsafe_current_a=self.failsafe_current,
            failsafe_timeout_s=self.failsafe_timeout,
            charge_current_a=current,
        )
        await self._async_resync_phase_after_reconnect()
        self._initialized_connection_epoch = self.client.stats.connection_epoch

    async def _async_resync_phase_after_reconnect(self) -> None:
        if self.requested_phase not in {"1", "3"}:
            return

        desired_raw = 0 if self.requested_phase == "1" else 1
        try:
            actual_raw = int(await self.client.read(PHASE_SWITCH))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not read phase switch after reconnect: %s", err)
            actual_raw = None

        if actual_raw == desired_raw:
            return

        await self.client.write(PHASE_SWITCH, desired_raw)
        _LOGGER.info(
            "Reasserted phase switch after Modbus reconnect: %sP",
            self.requested_phase,
        )

    async def async_shutdown(self) -> None:
        self._cancel_recovery()
        await self.client.async_close()

    @property
    def rest_restarting(self) -> bool:
        return time.monotonic() < self.rest_restart_until

    @property
    def recovery_active(self) -> bool:
        return self._recovery_task is not None or self.recovery_status in {
            _RECOVERY_OBSERVING,
            _RECOVERY_DWELLING,
            _RECOVERY_RESUMING,
        }

    def mark_rest_restart(self, seconds: int) -> None:
        self.rest_restart_until = time.monotonic() + seconds
        self.async_update_listeners()

    def record_rest_restart(self, result: str, reason: str | None = None) -> None:
        self.last_rest_restart_at = dt_util.utcnow()
        self.last_rest_restart_result = result
        self.last_rest_restart_reason = reason
        self.async_update_listeners()

    def phase_mismatch(self, data: ChargerSnapshot | None = None) -> bool:
        snapshot = data if data is not None else self.data
        requested_3p = self._phase_explicitly_requested and self.requested_phase == "3"
        return bool(snapshot and snapshot.available and phase_mismatch(snapshot, requested_3p))

    async def async_reassert_current(self) -> None:
        """Re-write the charge current the controller last asked for.

        Needed after an action that went over the charger's web UI instead of
        Modbus (the 3-phase config restore): the charger drops its charge current
        on such a config change, and evcc will not necessarily re-send its value,
        so a plugged-in car would sit at 0 A.
        """
        current = self.current_intent
        if self._buffer_commands or self.enabled_intent is False:
            current = 0
        if current is None:
            current = self.resume_current
        async with self._command_lock:
            await self.client.write(CURRENT_LIMIT, current)
        _LOGGER.info("Re-asserted charge current after web-UI action: %sA", current)
        await self.async_request_refresh()

    async def async_set_current(self, value: float) -> None:
        requested = normalize_current_a(value, self.max_current)
        if requested > 0:
            self.resume_current = requested
        self.current_intent = requested
        self.enabled_intent = requested > 0
        self.async_update_listeners()
        if self._buffer_commands:
            self._pending_current = requested
            self._pending_enabled = requested > 0
            if requested == 0:
                async with self._command_lock:
                    await self.client.write(CURRENT_LIMIT, 0)
                await self.async_request_refresh()
            return
        async with self._command_lock:
            await self.client.write(CURRENT_LIMIT, requested)
        await self.async_request_refresh()

    async def async_set_enabled(self, enabled: bool) -> None:
        self.enabled_intent = enabled
        if self._buffer_commands:
            self._pending_enabled = enabled
            if not enabled:
                self._pending_current = 0
                self.current_intent = 0
                async with self._command_lock:
                    await self.client.write(CURRENT_LIMIT, 0)
                await self.async_request_refresh()
            else:
                self.current_intent = self.current_intent or self.resume_current
                self.async_update_listeners()
            return
        await self.async_set_current(self.resume_current if enabled else 0)

    async def async_set_phase(self, option: str) -> None:
        if option not in {"1", "3"}:
            raise ValueError("Phase option must be '1' or '3'")
        self.requested_phase = option
        self._phase_explicitly_requested = True
        if option == "1":
            self._recovery_attempted = False
            self._cancel_recovery()
        async with self._command_lock:
            await self.client.write(PHASE_SWITCH, 0 if option == "1" else 1)
        await self.async_request_refresh()
        if option == "3":
            self._maybe_start_phase_recovery()

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

    async def _run_phase_recovery(self) -> None:
        try:
            self._set_recovery_status(_RECOVERY_OBSERVING, self.phase_recovery_observe)
            await self._countdown(self.phase_recovery_observe)
            data = self.data
            if data is None or not data.available or not data.vehicle_connected:
                self._finish_recovery(_RECOVERY_ABORTED, "vehicle disconnected during observation")
                return
            if not data.charging_active or self._measured_three_phase(data):
                self._finish_recovery(_RECOVERY_COMPLETE, "measured 3p during observation")
                return

            self._recovery_attempted = True
            self._buffer_commands = True
            self._pending_current = self.current_intent or self.resume_current
            self._pending_enabled = self.enabled_intent if self.enabled_intent is not None else True

            _LOGGER.info(
                "1->3 phase recovery: live switch did not result in measured 3P within %ss; "
                "holding charge current at 0A for %ss",
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
            _LOGGER.info("1->3 phase recovery: resumed with %sA", current)
            self._finish_recovery(_RECOVERY_COMPLETE, "dwell completed")
            await self.async_request_refresh()
        except asyncio.CancelledError:
            if self.requested_phase == "1":
                self._set_recovery_status(_RECOVERY_IDLE, 0)
            else:
                self._finish_recovery(_RECOVERY_ABORTED, "cancelled")
            raise
        except Exception:
            _LOGGER.exception("1->3 phase recovery failed")
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
        self._set_recovery_status(status, 0)

    def evcc_current_limit(self, data: ChargerSnapshot) -> int | None:
        return self.current_intent if self.current_intent is not None else data.current_limit_a

    def evcc_enabled(self, data: ChargerSnapshot) -> bool:
        return self.enabled_intent if self.enabled_intent is not None else data.enabled

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
