from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time

from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from homeassistant.const import CONF_HOST
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .control import is_three_phase_install, phase_mismatch, should_restore_phase_config
from .const import (
    CONF_GRID_PHASES,
    CONF_PHASE_RESTORE_DELAY,
    CONF_PHASE_RESTORE_ON_UNPLUG,
    CONF_REST_ENABLED,
    CONF_REST_PASSWORD,
    CONF_REST_USERNAME,
    DEFAULT_PHASE_RESTORE_DELAY_S,
    DEFAULT_PHASE_RESTORE_ON_UNPLUG,
    DEFAULT_REST_ENABLED,
    DEFAULT_REST_USERNAME,
    DEFAULT_RESUME_CURRENT,
    MAX_PHASE_RESTORE_DELAY_S,
    MIN_PHASE_RESTORE_DELAY_S,
    DOMAIN,
)
from homeassistant.helpers.storage import Store
from .eventlog import EventLog
from .rest_client import UnitePhpRestClient, UniteRestError, async_restore_three_phase, async_set_lockable_cable
from .modbus import WebastoBridgeClient
from .models import ChargerSnapshot, normalize_current_a
from .phase import (
    _RECOVERY_ABORTED,
    _RECOVERY_COMPLETE,
    _RECOVERY_DWELLING,
    _RECOVERY_IDLE,
    _RECOVERY_OBSERVING,
    _RECOVERY_RESUMING,
    PhaseRecoveryMixin,
)
from .registers import CURRENT_LIMIT, PHASE_SWITCH, FIRMWARE_VERSION, SERIAL_NUMBER
from .safety import capture_baseline, program_connection_ownership, restore_baseline, write_heartbeat

_LOGGER = logging.getLogger(__name__)


def effective_poll_interval_s(poll_interval: int, failsafe_timeout: int) -> int:
    return min(poll_interval, max(3, failsafe_timeout // 2))


class WebastoEvccCoordinator(PhaseRecoveryMixin, DataUpdateCoordinator[ChargerSnapshot]):
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
        # Lockable-cable installation setting (web UI). None = unknown
        # (no web UI login, or the firmware lacks the setting).
        self.lockable_cable: bool | None = None
        self._command_lock = asyncio.Lock()
        self._recovery_task: asyncio.Task[None] | None = None
        self._recovery_attempted = False
        self._downshift_attempted = False  # latch: one escalation per 1P request
        self._guard_mismatches = 0  # trede 1: consecutive wish-vs-405 polls
        self._recovery_deadline: float | None = None
        self._buffer_commands = False
        self._baseline_store = Store(hass, 1, f"{DOMAIN}_baseline_{entry.entry_id}")
        self._baseline: dict[str, int | None] | None = None
        self._baseline_loaded = False
        self._pending_current: int | None = None
        self._pending_enabled: bool | None = None
        self.rest_restart_until = 0.0
        self.last_recovery_at = None
        self.last_recovery_result: str | None = None
        self.last_recovery_reason: str | None = None
        self.last_rest_restart_at = None
        self.last_rest_restart_result: str | None = None
        self.last_rest_restart_reason: str | None = None
        # Static identity, read once at setup (best effort, tolerant decoding).
        self.device_serial_number: str | None = None
        self.device_firmware_version: str | None = None
        # Diagnostics-only event ring buffer (never read back for control).
        self.event_log = EventLog()

    def record_event(self, kind: str, detail: str) -> None:
        """Append a diagnostics-only event to the in-memory ring buffer."""
        self.event_log.record(kind, detail)

    async def async_read_device_info(self) -> None:
        """Read static identity once at setup (best effort).

        Same hardware and register map as the charger integration: serial and
        firmware version are plain Modbus strings decoded tolerantly (ASCII or
        NUL-interleaved UTF-16). Missing fields stay None and never fail setup.
        """
        self.device_serial_number = await self.client.read_optional_string_once(SERIAL_NUMBER)
        self.device_firmware_version = await self.client.read_optional_string_once(FIRMWARE_VERSION)

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
        just_connected = data.vehicle_connected and not self._vehicle_was_connected
        just_unplugged = self._vehicle_was_connected and not data.vehicle_connected
        self._vehicle_was_connected = data.vehicle_connected
        if not data.vehicle_connected:
            self._recovery_attempted = False
            self._downshift_attempted = False
            self._guard_mismatches = 0
        self._maybe_auto_restore_phase(data, just_unplugged)
        # A new session: the wallbox applies its own (minimum) charge current, so
        # put back what evcc actually asked for - 0 A when it wants no charging.
        if just_connected and not self.recovery_active:
            try:
                await self.async_reassert_current("a new session", refresh=False)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Could not re-assert current at session start: %s", err)
            try:
                await self._async_reassert_phase(context="session")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Could not re-assert phase at session start: %s", err)

        await self._guard_phase_setting(data)

        try:
            await write_heartbeat(self.client)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Heartbeat failed: %s", err)
            return ChargerSnapshot(available=False, last_error=str(err))

        return data

    def _maybe_auto_restore_phase(self, data: ChargerSnapshot, just_unplugged: bool) -> None:
        """Re-apply the installation phase config after the vehicle is unplugged.

        The firmware only resets register 405 to its default on a power cycle,
        reset or Modbus disconnect - never on unplug - so on some chargers a new
        session starts single-phase even with 3-phase configured and every
        register reading correctly. Toggling currentLimiterPhase after each unplug
        forces the firmware to re-apply its default, giving the next session a
        clean start.

        Edge-triggered on the unplug: the fix would tear down a running session,
        so it only runs with no vehicle attached, and firing once per unplug means
        no pacing or retry counter - a failed attempt is retried at the next
        unplug.
        """
        if not just_unplugged:
            return
        o = self.entry.options
        if not should_restore_phase_config(
            enabled=o.get(CONF_PHASE_RESTORE_ON_UNPLUG, DEFAULT_PHASE_RESTORE_ON_UNPLUG),
            rest_enabled=o.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED),
            vehicle_connected=data.vehicle_connected,
            phase_capability_raw=data.phase_capability_raw,
            grid_phases=o.get(CONF_GRID_PHASES),
        ):
            return
        if self._auto_restore_task is not None and not self._auto_restore_task.done():
            return
        self._auto_restore_task = self.hass.async_create_task(self._async_auto_restore_phase())

    async def _async_auto_restore_phase(self) -> None:
        o = self.entry.options
        host = o.get(CONF_HOST, self.entry.data[CONF_HOST])
        delay = int(o.get(CONF_PHASE_RESTORE_DELAY, DEFAULT_PHASE_RESTORE_DELAY_S))
        delay = max(MIN_PHASE_RESTORE_DELAY_S, min(MAX_PHASE_RESTORE_DELAY_S, delay))
        # Let the charger finish ending the session and settle; also debounces a
        # bouncing cable state on unplug. Only re-check for a reconnect when we
        # actually waited: with no delay there is no window, and self.data may not
        # yet hold this poll's (just-unplugged) snapshot.
        if delay:
            await asyncio.sleep(delay)
            # A vehicle reconnected during the settle delay: toggling now would
            # drop register 404 to 0 for ~10 s and tear down the starting
            # session. Abort.
            if self.data is not None and self.data.vehicle_connected:
                _LOGGER.debug(
                    "Vehicle reconnected during the settle delay; skipping phase restore"
                )
                return
        try:
            route = await async_restore_three_phase(
                async_get_clientsession(self.hass),
                host,
                o.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME),
                o.get(CONF_REST_PASSWORD, ""),
            )
        except UniteRestError as err:
            _LOGGER.warning(
                "Automatic phase-config restore after unplug failed "
                "(will retry at the next unplug): %s",
                err,
            )
            self.record_event("phase_restore_failed", str(err))
            return
        self.record_event("phase_restore", f"via {route}")
        _LOGGER.info(
            "Vehicle unplugged; re-applied the 3-phase config via %s so the next "
            "session starts clean",
            route,
        )
        await self.async_request_refresh()

    async def _async_ensure_connection_ownership(self) -> None:
        if (
            self.client.stats.connected
            and self._initialized_connection_epoch == self.client.stats.connection_epoch
        ):
            return

        self.record_event(
            "reconnect", f"connection epoch {self.client.stats.connection_epoch}"
        )

        current = self.current_intent
        if self._buffer_commands or self.enabled_intent is False:
            current = 0
        if current is None:
            current = 0

        # Capture the pre-integration register values once, before our first
        # write. Stored durably, so a restart never mistakes our own values
        # for the originals.
        if not self._baseline_loaded:
            self._baseline_loaded = True
            try:
                stored = await self._baseline_store.async_load()
            except Exception:  # noqa: BLE001 - a corrupt store must not break setup
                stored = None
            if isinstance(stored, dict):
                self._baseline = {
                    str(k): (int(v) if isinstance(v, (int, float)) else None)
                    for k, v in stored.items()
                }
        if self._baseline is None:
            self._baseline = await capture_baseline(self.client)
            try:
                await self._baseline_store.async_save(self._baseline)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not persist the register baseline", exc_info=True)
            _LOGGER.info("Captured charger register baseline: %s", self._baseline)

        await program_connection_ownership(
            self.client,
            failsafe_current_a=self.failsafe_current,
            failsafe_timeout_s=self.failsafe_timeout,
            charge_current_a=current,
        )
        await self._async_resync_phase_after_reconnect()
        self._initialized_connection_epoch = self.client.stats.connection_epoch

    async def _async_resync_phase_after_reconnect(self) -> None:
        """Re-assert the requested phase after a Modbus reconnect."""
        await self._async_reassert_phase(context="reconnect")

    async def _async_reassert_phase(self, *, context: str) -> None:
        """Read register 405 and write the requested phase back if it drifted.

        Read-then-write, idempotent: only when evcc asked for an explicit 1P/3P
        and the measured register differs do we write. ``context`` only selects
        the event kind recorded on an actual write ("reconnect" vs "session").
        """
        if self.requested_phase not in {"1", "3"}:
            return

        desired_raw = 0 if self.requested_phase == "1" else 1
        try:
            measured_raw = int(await self.client.read(PHASE_SWITCH))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not read phase switch (%s): %s", context, err)
            return

        if measured_raw == desired_raw:
            return

        await self.client.write(PHASE_SWITCH, desired_raw)
        if context == "session":
            self.record_event(
                "phase_reassert_session",
                f"requested={self.requested_phase}P measured={measured_raw} "
                f"written={desired_raw}",
            )
            _LOGGER.info(
                "Reasserted phase switch at session start: %sP", self.requested_phase
            )
        else:
            self.record_event(
                "405_write",
                f"reconnect reassert {self.requested_phase}P (reg={desired_raw})",
            )
            _LOGGER.info(
                "Reasserted phase switch after Modbus reconnect: %sP",
                self.requested_phase,
            )

    async def async_shutdown(self) -> None:
        self._cancel_recovery()
        try:
            await self.async_restore_baseline_on_exit()
        except Exception:  # noqa: BLE001 - shutdown must still close the client
            _LOGGER.exception("Baseline restore on shutdown failed")
        await self.client.async_close()

    def _webconfig_client(self) -> UnitePhpRestClient | None:
        """Webconfig client when the web UI login is configured, else None."""
        o = self.entry.options
        if not o.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED):
            return None
        return UnitePhpRestClient(
            o.get(CONF_HOST, self.entry.data.get(CONF_HOST, "")),
            o.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME),
            o.get(CONF_REST_PASSWORD, ""),
        )

    async def async_refresh_lockable_cable(self) -> None:
        """Read the lockable-cable installation setting (best effort)."""
        client = self._webconfig_client()
        if client is None:
            return
        try:
            value = await client.get_lockable_cable()
        except UniteRestError as err:
            _LOGGER.debug("Could not read lockable-cable setting: %s", err)
            return
        if value is not None and value != self.lockable_cable:
            self.lockable_cable = value
            self.async_update_listeners()

    async def async_set_lockable_cable(self, enabled: bool) -> None:
        """Write the lockable-cable setting and verify by read-back.

        Writes prefer the JSON config API with webconfig fallback; verification
        reads back over webconfig (the JSON API has no read endpoint). When no
        read-back is possible, the acknowledged write stands.
        """
        o = self.entry.options
        if not o.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED):
            raise UniteRestError("Web UI login is not configured")
        route = await async_set_lockable_cable(
            async_get_clientsession(self.hass),
            o.get(CONF_HOST, self.entry.data.get(CONF_HOST, "")),
            o.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME),
            o.get(CONF_REST_PASSWORD, ""),
            enabled,
        )
        value: bool | None = None
        php = self._webconfig_client()
        if php is not None:
            try:
                value = await php.get_lockable_cable()
            except UniteRestError as err:
                _LOGGER.debug("Could not verify the lockable-cable setting: %s", err)
        if value is None:
            _LOGGER.debug("Set lockable cable via %s without read-back", route)
            value = enabled
        self.lockable_cable = value
        self.async_update_listeners()
        if value != enabled:
            raise UniteRestError("Charger did not take the lockable-cable setting")

    async def async_restore_baseline_on_exit(self) -> None:
        """Write the captured pre-integration values back (best effort).

        Called on unload, removal and shutdown. Never raises; anything that
        cannot be restored is logged with its value for manual recovery.
        """
        baseline = self._baseline
        if baseline is None:
            try:
                stored = await self._baseline_store.async_load()
            except Exception:  # noqa: BLE001
                stored = None
            baseline = dict(stored) if isinstance(stored, dict) else None
        if not baseline or all(v is None for v in baseline.values()):
            return
        failed = await restore_baseline(self.client, baseline)
        if failed:
            _LOGGER.warning(
                "Could not restore charger registers; recover manually: %s",
                {key: baseline.get(key) for key in failed},
            )
            self.record_event("baseline_restore_failed", str(failed))
        else:
            _LOGGER.info("Restored charger register baseline on exit")
            self.record_event("baseline_restore", "ok")

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

    async def async_reassert_current(self, reason: str = "web-UI action", *, refresh: bool = True) -> None:
        """Re-write the charge current the controller last asked for.

        Needed whenever the charger may have set its own current without Modbus
        seeing it: after a web-UI config change, and at the start of a session
        (the wallbox applies its hardware minimum when a vehicle is plugged in).
        evcc will not necessarily re-send its value, so without this a car can
        charge while evcc has asked for nothing.
        """
        current = self.current_intent
        if self._buffer_commands or self.enabled_intent is False:
            current = 0
        if current is None:
            # evcc has not commanded anything yet: hold 0 A, never resume_current.
            # A fresh session must not start charging before evcc has an intent.
            current = 0
        async with self._command_lock:
            await self.client.write(CURRENT_LIMIT, current)
        _LOGGER.info("Re-asserted charge current after %s: %sA", reason, current)
        if refresh:
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
        else:
            self._downshift_attempted = False  # a 3P request re-arms downshift
        async with self._command_lock:
            await self.client.write(PHASE_SWITCH, 0 if option == "1" else 1)
        self.record_event(
            "405_write", f"evcc {option}P (reg={0 if option == '1' else 1})"
        )
        await self.async_request_refresh()
        if option == "3":
            self._maybe_start_phase_recovery()
        else:
            self._maybe_start_phase_downshift()


    def evcc_current_limit(self, data: ChargerSnapshot) -> int | None:
        return self.current_intent if self.current_intent is not None else data.current_limit_a

    def evcc_enabled(self, data: ChargerSnapshot) -> bool:
        return self.enabled_intent if self.enabled_intent is not None else data.enabled

