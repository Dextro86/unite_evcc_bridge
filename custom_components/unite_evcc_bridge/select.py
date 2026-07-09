from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
    async_add_entities([WebastoPhaseModeSelect(coordinator, entry.entry_id)])


class WebastoPhaseModeSelect(WebastoEvccEntity, SelectEntity):
    _attr_translation_key = "phase_mode"
    _attr_options = ["1", "3"]

    def __init__(self, coordinator: WebastoEvccCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "phase_mode")

    @property
    def current_option(self) -> str | None:
        data = self.coordinator.data
        if data is None or not data.available:
            return None
        return self.coordinator.requested_phase or data.phase_mode

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_phase(option)
