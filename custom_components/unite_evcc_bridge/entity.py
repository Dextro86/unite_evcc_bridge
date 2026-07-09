from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WebastoEvccCoordinator


class WebastoEvccEntity(CoordinatorEntity[WebastoEvccCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: WebastoEvccCoordinator, entry_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Unite EVCC Bridge",
            manufacturer="Webasto / Vestel",
            model="Unite / EVC-04",
            # "Visit device" link on the device page -> opens the charger web UI.
            configuration_url=f"http://{coordinator.client.host}",
        )

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return bool(data and data.available)
