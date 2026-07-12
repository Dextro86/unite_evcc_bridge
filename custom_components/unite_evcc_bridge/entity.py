from __future__ import annotations

from homeassistant.components.binary_sensor import (
    ENTITY_ID_FORMAT as BINARY_SENSOR_FORMAT,
    BinarySensorEntity,
)
from homeassistant.components.button import (
    ENTITY_ID_FORMAT as BUTTON_FORMAT,
    ButtonEntity,
)
from homeassistant.components.number import (
    ENTITY_ID_FORMAT as NUMBER_FORMAT,
    NumberEntity,
)
from homeassistant.components.select import (
    ENTITY_ID_FORMAT as SELECT_FORMAT,
    SelectEntity,
)
from homeassistant.components.sensor import (
    ENTITY_ID_FORMAT as SENSOR_FORMAT,
    SensorEntity,
)
from homeassistant.components.switch import (
    ENTITY_ID_FORMAT as SWITCH_FORMAT,
    SwitchEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WebastoEvccCoordinator

# Each platform entity type paired with its "<domain>.{}" entity_id template.
_ENTITY_ID_FORMATS: tuple[tuple[type, str], ...] = (
    (BinarySensorEntity, BINARY_SENSOR_FORMAT),
    (ButtonEntity, BUTTON_FORMAT),
    (NumberEntity, NUMBER_FORMAT),
    (SelectEntity, SELECT_FORMAT),
    (SwitchEntity, SWITCH_FORMAT),
    (SensorEntity, SENSOR_FORMAT),
)


class WebastoEvccEntity(CoordinatorEntity[WebastoEvccCoordinator]):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WebastoEvccCoordinator,
        entry_id: str,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_{key}"
        # Force a stable, language-independent entity_id from the English key
        # (e.g. sensor.unite_evcc_bridge_iec61851_status) instead of letting HA
        # derive it from the localized display name, so the entity_ids you paste
        # into evcc are identical regardless of your HA language. The platform is
        # taken from the concrete entity type this base is mixed into.
        for entity_type, id_format in _ENTITY_ID_FORMATS:
            if isinstance(self, entity_type):
                self.entity_id = async_generate_entity_id(
                    id_format, f"unite_evcc_bridge_{key}", hass=coordinator.hass
                )
                break
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
