from __future__ import annotations

import time

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_REST_ENABLED,
    CONF_REST_PASSWORD,
    CONF_REST_USERNAME,
    DEFAULT_REST_ENABLED,
    DEFAULT_REST_USERNAME,
    DOMAIN,
    REST_RESTART_COOLDOWN_S,
)
from .coordinator import WebastoEvccCoordinator
from .entity import WebastoEvccEntity
from .rest_client import UniteRestAuthError, UniteRestError, async_restart_charger


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if not bool(entry.options.get(CONF_REST_ENABLED, DEFAULT_REST_ENABLED)):
        return

    coordinator: WebastoEvccCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WebastoRestartButton(coordinator, entry)])


class WebastoRestartButton(WebastoEvccEntity, ButtonEntity):
    _attr_translation_key = "restart"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: WebastoEvccCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry.entry_id, "restart")
        self._entry = entry
        self._last_press = 0.0

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        now = time.monotonic()
        remaining = REST_RESTART_COOLDOWN_S - (now - self._last_press)
        if remaining > 0:
            self.coordinator.record_rest_restart("cooldown", f"{int(remaining)}s remaining")
            raise HomeAssistantError(
                f"Restart was already requested. Wait {int(remaining)} seconds before trying again."
            )

        self._last_press = now
        host = self._entry.options.get(CONF_HOST, self._entry.data[CONF_HOST])
        username = self._entry.options.get(CONF_REST_USERNAME, DEFAULT_REST_USERNAME)
        password = self._entry.options.get(CONF_REST_PASSWORD, "")
        session = async_get_clientsession(self.hass)
        try:
            await async_restart_charger(session, host, username, password)
        except UniteRestAuthError as err:
            self.coordinator.record_rest_restart("auth_failed", str(err))
            raise HomeAssistantError(f"Could not restart charger via web UI: {err}") from err
        except UniteRestError as err:
            self.coordinator.record_rest_restart("unreachable", str(err))
            raise HomeAssistantError(f"Could not restart charger via web UI: {err}") from err
        self.coordinator.record_rest_restart("success")
        self.coordinator.mark_rest_restart(REST_RESTART_COOLDOWN_S)
