from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant

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

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if coordinator.data is None:
        hass.async_create_task(coordinator.async_request_refresh())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if coordinator is not None:
        await coordinator.async_shutdown()
    return unload_ok
