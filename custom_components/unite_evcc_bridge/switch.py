from __future__ import annotations

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WebastoEvccCoordinator
from .entity import WebastoEvccEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WebastoEvccCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WebastoChargingEnabledSwitch(coordinator, entry.entry_id)])


class WebastoChargingEnabledSwitch(WebastoEvccEntity, SwitchEntity):
    _attr_translation_key = "charging_enabled"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: WebastoEvccCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "charging_enabled")

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None or not data.available:
            return None
        return self.coordinator.evcc_enabled(data)

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_enabled(False)
