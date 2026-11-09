"""Binärsensor: ist der dynamische Tarif aktuell im Gesamtzeitraum günstiger?"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DynamicTariffAuditorConfigEntry
from .coordinator import DynamicTariffAuditorCoordinator
from .entity import DynamicTariffAuditorEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DynamicTariffAuditorConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([DynamicCheaperBinarySensor(entry.runtime_data)])


class DynamicCheaperBinarySensor(DynamicTariffAuditorEntity, BinarySensorEntity):
    _attr_translation_key = "dynamic_cheaper"

    def __init__(self, coordinator: DynamicTariffAuditorCoordinator) -> None:
        super().__init__(coordinator, "dynamic_cheaper")

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        if not data.get("kwh"):
            return None  # noch keine Daten
        return data["difference"] > 0
