from __future__ import annotations

from homeassistant.components.number import (
    ENTITY_ID_FORMAT,
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
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
    async_add_entities([WebastoMaximumCurrentNumber(coordinator, entry.entry_id)])


class WebastoMaximumCurrentNumber(WebastoEvccEntity, NumberEntity):
    _attr_translation_key = "maximum_current"
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: WebastoEvccCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "maximum_current", ENTITY_ID_FORMAT)
        self._attr_native_max_value = coordinator.max_current

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if data is None or not data.available:
            return None
        return self.coordinator.evcc_current_limit(data)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_current(value)
