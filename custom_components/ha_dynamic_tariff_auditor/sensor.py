"""Sensoren: Kosten, Differenz, Ø-Preise, Verbrauch."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import DynamicTariffAuditorEntity
from .coordinator import DynamicTariffAuditorCoordinator
from . import DynamicTariffAuditorConfigEntry

EUR = "€"

SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="cfg_work_static_ct", translation_key="cfg_work_static_ct",
        native_unit_of_measurement="ct/kWh", state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="cfg_base_static", translation_key="cfg_base_static",
        native_unit_of_measurement="€/Monat", state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="cfg_base_dyn", translation_key="cfg_base_dyn",
        native_unit_of_measurement="€/Monat", state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="cfg_surcharge_dyn_ct", translation_key="cfg_surcharge_dyn_ct",
        native_unit_of_measurement="ct/kWh", state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="month_dyn_cost", translation_key="month_dyn_cost",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement=EUR,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="month_static_cost", translation_key="month_static_cost",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement=EUR,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="month_difference", translation_key="month_difference",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement=EUR,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="dyn_cost", translation_key="dyn_cost",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement=EUR,
        state_class=SensorStateClass.TOTAL,
    ),
    SensorEntityDescription(
        key="static_cost", translation_key="static_cost",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement=EUR,
        state_class=SensorStateClass.TOTAL,
    ),
    SensorEntityDescription(
        key="difference", translation_key="difference",
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement=EUR,
        state_class=SensorStateClass.TOTAL,
    ),
    SensorEntityDescription(
        key="savings_pct", translation_key="savings_pct",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="kwh", translation_key="kwh",
        device_class=SensorDeviceClass.ENERGY, native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="avg_dyn_ct", translation_key="avg_dyn_ct",
        native_unit_of_measurement="ct/kWh", state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="avg_static_ct", translation_key="avg_static_ct",
        native_unit_of_measurement="ct/kWh", state_class=SensorStateClass.MEASUREMENT,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DynamicTariffAuditorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(DynamicTariffAuditorSensor(coordinator, d) for d in SENSORS)


class DynamicTariffAuditorSensor(DynamicTariffAuditorEntity, SensorEntity):
    def __init__(self, coordinator: DynamicTariffAuditorCoordinator, description: SensorEntityDescription):
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_suggested_display_precision = 2

    @property
    def native_value(self) -> Any:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self.coordinator.data:
            return None
        if self.entity_description.key == "dyn_cost":
            # Monats- und Jahreswerte inkl. anteiliger Grundgebühr (für Karten/Auswertungen)
            return {"monate": self.coordinator.data.get("monate", {}),
                    "jahre": self.coordinator.data.get("jahre", {})}
        if self.entity_description.key == "difference":
            # Aufschlüsselung je Tarifzeitraum (mehrere Gültigkeitszeiträume)
            return {"zeitraeume": self.coordinator.data.get("buckets", {})}
        return None
