"""Dynamic Tariff Auditor: dynamischer Stromtarif vs. statische Tarife."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import DynamicTariffAuditorCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

type DynamicTariffAuditorConfigEntry = ConfigEntry[DynamicTariffAuditorCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DynamicTariffAuditorConfigEntry) -> bool:
    coordinator = DynamicTariffAuditorCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DynamicTariffAuditorConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
