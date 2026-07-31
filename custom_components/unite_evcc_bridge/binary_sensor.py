from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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
    async_add_entities(
        [
            UniteEvccConnectionSensor(coordinator, entry.entry_id),
            UniteEvccPhaseMismatchSensor(coordinator, entry.entry_id),
        ]
    )


class UniteEvccConnectionSensor(WebastoEvccEntity, BinarySensorEntity):
    """Modbus connection health. Stays available so it can report 'off'."""

    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WebastoEvccCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "connection")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        return bool(data and data.available)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        stats = self.coordinator.client.stats
        return {
            "reconnects": stats.reconnects,
            "read_failures": stats.read_failures,
            "write_failures": stats.write_failures,
            "timeouts": stats.timeouts,
            "alive_failures": stats.alive_failures,
            "last_response_ms": stats.last_response_ms,
            "avg_response_ms": round(stats.avg_response_ms, 1) if stats.avg_response_ms is not None else None,
            "configured_poll_interval": self.coordinator.configured_poll_interval,
            "effective_poll_interval": self.coordinator.effective_poll_interval,
            "last_error": stats.last_error,
        }


class UniteEvccPhaseMismatchSensor(WebastoEvccEntity, BinarySensorEntity):
    _attr_translation_key = "phase_mismatch"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WebastoEvccCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "phase_mismatch")

    @property
    def is_on(self) -> bool:
        # Routed through the coordinator so the "3-phase actually requested" gate
        # is applied (a resting 405=3 on a 1-phase car is not a mismatch).
        return self.coordinator.phase_mismatch()
