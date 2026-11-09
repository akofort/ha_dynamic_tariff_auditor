"""Dynamic Tariff Auditor: dynamischer Stromtarif vs. statische Tarife."""
from __future__ import annotations

from pathlib import Path

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import import_statistics
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .calc import PRICE_UNIT_FACTORS
from .const import CONF_PRICE, CONF_PRICE_UNIT, DOMAIN
from .coordinator import RESET_PENDING, STORAGE_VERSION, DynamicTariffAuditorCoordinator
from .smard import parse_smard_csv

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]
SERVICE_IMPORT_SMARD = "import_smard_prices"

type DynamicTariffAuditorConfigEntry = ConfigEntry[DynamicTariffAuditorCoordinator]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Dienst registrieren (einmalig, unabhängig vom Eintrag)."""

    async def _handle_import(call: ServiceCall) -> None:
        await _async_import_smard(hass, call.data["file"])

    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_SMARD):
        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_SMARD,
            _handle_import,
            schema=vol.Schema({vol.Required("file"): str}),
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DynamicTariffAuditorConfigEntry) -> bool:
    coordinator = DynamicTariffAuditorCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DynamicTariffAuditorConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: DynamicTariffAuditorConfigEntry) -> None:
    """Tarife/Optionen geändert: gespeicherten Stand verwerfen, ab Auswertungsbeginn neu rechnen."""
    await _async_reset_and_reload(hass, entry)


async def _async_reset_and_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    RESET_PENDING.add(entry.entry_id)
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    await hass.config_entries.async_reload(entry.entry_id)


def _read_file(path: str) -> str:
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


async def _async_import_smard(hass: HomeAssistant, path: str) -> None:
    """SMARD-Preise als Stundenmittel in die Statistik des Preissensors schreiben."""
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
    if not entries:
        raise vol.Invalid("Kein Eintrag von Dynamic Tariff Auditor geladen")
    entry = entries[0]
    tz = dt_util.get_default_time_zone()
    text = await hass.async_add_executor_job(_read_file, path)
    prices = parse_smard_csv(text, tz)
    if not prices:
        raise vol.Invalid("Keine gültigen Preise in der Datei")

    # SMARD liefert €/MWh; in die Einheit des Preissensors umrechnen
    unit = entry.data[CONF_PRICE_UNIT]
    factor = PRICE_UNIT_FACTORS["€/MWh"] / PRICE_UNIT_FACTORS[unit]
    metadata = StatisticMetaData(
        source="recorder",
        statistic_id=entry.data[CONF_PRICE],
        name=None,
        unit_of_measurement=unit,
        has_mean=True,
        mean_type=StatisticMeanType.ARITHMETIC,
        has_sum=False,
    )
    stats = [StatisticData(start=hour, mean=value * factor) for hour, value in prices.items()]
    await get_instance(hass).async_add_executor_job(
        import_statistics, get_instance(hass), metadata, stats
    )
    await _async_reset_and_reload(hass, entry)
