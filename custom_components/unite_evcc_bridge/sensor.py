from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .control import CHARGER_STATE_OPTIONS, derive_state
from .const import DOMAIN
from .coordinator import WebastoEvccCoordinator
from .entity import WebastoEvccEntity
from .models import ChargerSnapshot


@dataclass(frozen=True, kw_only=True)
class BridgeSensorDescription(SensorEntityDescription):
    value_fn: Callable[[WebastoEvccCoordinator, ChargerSnapshot | None], str | int | float | datetime | None]
    attr_fn: Callable[[WebastoEvccCoordinator, ChargerSnapshot | None], dict[str, object]] | None = None
    options: list[str] | None = None
    require_available: bool = True


SENSORS: tuple[BridgeSensorDescription, ...] = (
    BridgeSensorDescription(
        key="charger_state",
        translation_key="charger_state",
        device_class=SensorDeviceClass.ENUM,
        options=CHARGER_STATE_OPTIONS,
        require_available=False,
        value_fn=lambda coordinator, data: derive_state(
            connection_ok=bool(coordinator.last_update_success and data and data.available),
            restarting=coordinator.rest_restarting,
            faulted=bool(data and data.available and data.faulted),
            vehicle_connected=bool(data and data.available and data.vehicle_connected),
            charging=bool(data and data.available and data.charging_active),
            phase_mismatch=coordinator.phase_mismatch(data),
            recovery_active=coordinator.recovery_active,
        ),
    ),
    BridgeSensorDescription(
        key="iec61851_status",
        translation_key="iec61851_status",
        value_fn=lambda coordinator, data: data.iec61851_status,
    ),
    BridgeSensorDescription(
        key="active_power",
        translation_key="active_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator, data: data.active_power_w,
    ),
    BridgeSensorDescription(
        key="energy_total",
        translation_key="energy_total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda coordinator, data: data.energy_total_kwh,
    ),
    BridgeSensorDescription(
        key="session_energy",
        translation_key="session_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda coordinator, data: data.session_energy_kwh,
    ),
    BridgeSensorDescription(
        key="current_l1",
        translation_key="current_l1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator, data: data.current_l1_a,
    ),
    BridgeSensorDescription(
        key="current_l2",
        translation_key="current_l2",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator, data: data.current_l2_a,
    ),
    BridgeSensorDescription(
        key="current_l3",
        translation_key="current_l3",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator, data: data.current_l3_a,
    ),
    BridgeSensorDescription(
        key="voltage_l1",
        translation_key="voltage_l1",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator, data: data.voltage_l1_v,
    ),
    BridgeSensorDescription(
        key="voltage_l2",
        translation_key="voltage_l2",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator, data: data.voltage_l2_v,
    ),
    BridgeSensorDescription(
        key="voltage_l3",
        translation_key="voltage_l3",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator, data: data.voltage_l3_v,
    ),
    BridgeSensorDescription(
        key="session_duration",
        translation_key="session_duration",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator, data: data.session_duration_s,
    ),
    BridgeSensorDescription(
        key="requested_phase",
        translation_key="requested_phase",
        value_fn=lambda coordinator, data: (
            f"{coordinator.requested_phase or data.phase_mode}P"
            if coordinator.requested_phase or data.phase_mode
            else None
        ),
    ),
    BridgeSensorDescription(
        key="hardware_current_limit",
        translation_key="hardware_current_limit",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator, data: data.current_limit_a,
    ),
    BridgeSensorDescription(
        key="register_404",
        translation_key="register_404",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator, data: data.phase_capability_raw,
    ),
    BridgeSensorDescription(
        key="register_405",
        translation_key="register_405",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator, data: data.phase_mode_raw,
    ),
    BridgeSensorDescription(
        key="phase_recovery_status",
        translation_key="phase_recovery_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator, data: coordinator.recovery_status,
    ),
    BridgeSensorDescription(
        key="phase_recovery_remaining",
        translation_key="phase_recovery_remaining",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator, data: coordinator.recovery_remaining_s,
    ),
    BridgeSensorDescription(
        key="last_recovery",
        translation_key="last_recovery",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        require_available=False,
        value_fn=lambda coordinator, data: coordinator.last_recovery_at,
        attr_fn=lambda coordinator, data: {
            "result": coordinator.last_recovery_result,
            "reason": coordinator.last_recovery_reason,
        },
    ),
    BridgeSensorDescription(
        key="last_restart",
        translation_key="last_restart",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        require_available=False,
        value_fn=lambda coordinator, data: coordinator.last_rest_restart_at,
        attr_fn=lambda coordinator, data: {
            "result": coordinator.last_rest_restart_result,
            "reason": coordinator.last_rest_restart_reason,
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WebastoEvccCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            *(
                WebastoEvccSensor(coordinator, entry.entry_id, description)
                for description in SENSORS
            ),
        ]
    )


class WebastoEvccSensor(WebastoEvccEntity, SensorEntity):
    entity_description: BridgeSensorDescription

    def __init__(
        self,
        coordinator: WebastoEvccCoordinator,
        entry_id: str,
        description: BridgeSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry_id, description.key)
        self.entity_description = description
        if description.options is not None:
            self._attr_options = description.options

    @property
    def available(self) -> bool:
        if not self.entity_description.require_available:
            return True
        return super().available

    @property
    def native_value(self) -> str | int | float | datetime | None:
        data = self.coordinator.data
        if self.entity_description.require_available and (data is None or not data.available):
            return None
        return self.entity_description.value_fn(self.coordinator, data)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        if self.entity_description.attr_fn is None:
            return None
        return self.entity_description.attr_fn(self.coordinator, self.coordinator.data)
