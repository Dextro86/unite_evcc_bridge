from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import EVENT_HOMEASSISTANT_STOP, HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_FAILSAFE_CURRENT,
    CONF_FAILSAFE_TIMEOUT,
    CONF_MAX_CURRENT,
    CONF_PHASE_RECOVERY_DWELL,
    CONF_PHASE_RECOVERY_ENABLED,
    CONF_PHASE_RECOVERY_OBSERVE,
    CONF_POLL_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_UNIT_ID,
    DEFAULT_FAILSAFE_CURRENT_A,
    DEFAULT_FAILSAFE_TIMEOUT_S,
    DEFAULT_MAX_CURRENT,
    DEFAULT_PHASE_RECOVERY_DWELL_S,
    DEFAULT_PHASE_RECOVERY_ENABLED,
    DEFAULT_PHASE_RECOVERY_OBSERVE_S,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_UNIT_ID,
    DOMAIN,
)
from .coordinator import WebastoEvccCoordinator
from .modbus import WebastoBridgeClient
from .registers import SERIAL_NUMBER

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
]


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    options = entry.options
    client = WebastoBridgeClient(
        options.get(CONF_HOST, entry.data[CONF_HOST]),
        int(options.get(CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT))),
        int(options.get(CONF_UNIT_ID, entry.data.get(CONF_UNIT_ID, DEFAULT_UNIT_ID))),
    )
    coordinator = WebastoEvccCoordinator(
        hass,
        entry=entry,
        client=client,
        poll_interval=int(
            options.get(
                CONF_POLL_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
            )
        ),
        max_current=int(options.get(CONF_MAX_CURRENT, entry.data.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT))),
        failsafe_current=int(options.get(CONF_FAILSAFE_CURRENT, DEFAULT_FAILSAFE_CURRENT_A)),
        failsafe_timeout=int(options.get(CONF_FAILSAFE_TIMEOUT, DEFAULT_FAILSAFE_TIMEOUT_S)),
        phase_recovery_enabled=bool(options.get(CONF_PHASE_RECOVERY_ENABLED, DEFAULT_PHASE_RECOVERY_ENABLED)),
        phase_recovery_observe=int(options.get(CONF_PHASE_RECOVERY_OBSERVE, DEFAULT_PHASE_RECOVERY_OBSERVE_S)),
        phase_recovery_dwell=int(options.get(CONF_PHASE_RECOVERY_DWELL, DEFAULT_PHASE_RECOVERY_DWELL_S)),
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Initial update failed; %s will keep retrying: %s", DOMAIN, err)

    await _async_maybe_repair_unique_id(hass, entry, client)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Read the lockable-cable installation setting once in the background; the
    # switch stays unavailable until it is known (or when no web UI login set).
    hass.async_create_task(coordinator.async_refresh_lockable_cable())

    async def _async_restore_on_stop(_event) -> None:
        coord = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coord is not None:
            try:
                await coord.async_restore_baseline_on_exit()
            except Exception:  # noqa: BLE001 - shutdown must never hang on this
                _LOGGER.exception("Baseline restore on shutdown failed")

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_restore_on_stop)
    )
    if coordinator.data is None:
        hass.async_create_task(coordinator.async_request_refresh())
    return True


async def _async_maybe_repair_unique_id(
    hass: HomeAssistant, entry: ConfigEntry, client: WebastoBridgeClient
) -> None:
    """One-time repair for entries created with the host as unique_id.

    A host ID duplicates when the charger's IP changes (or a retried setup
    read the serial the second time). Straighten to the serial once known;
    if the serial entry already exists this one is the duplicate and the
    user is told to remove one.
    """
    legacy = entry.data.get(CONF_HOST, "")
    if not legacy or entry.unique_id != legacy:
        return
    try:
        serial = await client._optional_string(SERIAL_NUMBER)
    except Exception:  # noqa: BLE001 - repair is best effort
        return
    serial = (serial or "").strip()
    if not serial or serial == legacy:
        return
    clash = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and e.unique_id == serial
    ]
    if clash:
        _LOGGER.warning(
            "Two integration entries point at the same charger (serial %s); "
            "remove one under Settings -> Devices & Services",
            serial,
        )
        return
    hass.config_entries.async_update_entry(entry, unique_id=serial)
    _LOGGER.info("Repaired integration entry to use the charger serial")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if coordinator is not None:
        await coordinator.async_shutdown()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry; drop its stored register baseline."""
    try:
        await Store(hass, 1, f"{DOMAIN}_baseline_{entry.entry_id}").async_remove()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not remove the stored register baseline", exc_info=True)
