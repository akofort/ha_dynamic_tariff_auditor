"""Dynamic Tariff Auditor: dynamischer Stromtarif vs. statische Tarife."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .coordinator import RESET_PENDING, STORAGE_VERSION, DynamicTariffAuditorCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

type DynamicTariffAuditorConfigEntry = ConfigEntry[DynamicTariffAuditorCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DynamicTariffAuditorConfigEntry) -> bool:
    coordinator = DynamicTariffAuditorCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: DynamicTariffAuditorConfigEntry) -> None:
    """Tarife/Optionen geändert: gespeicherten Stand verwerfen, ab Auswertungsbeginn neu rechnen."""
    RESET_PENDING.add(entry.entry_id)
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: DynamicTariffAuditorConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
