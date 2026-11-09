"""Gemeinsame Basis für Sensoren und Binärsensoren."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DynamicTariffAuditorCoordinator


class DynamicTariffAuditorEntity(CoordinatorEntity[DynamicTariffAuditorCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: DynamicTariffAuditorCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=coordinator.config_entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )
