"""Dynamic Tariff Auditor: dynamischer Stromtarif vs. statische Tarife."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.db_schema import Statistics
from homeassistant.components.recorder.statistics import import_statistics
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .calc import PRICE_UNIT_FACTORS
from .const import CONF_PRICE, CONF_PRICE_UNIT, DOMAIN
from .coordinator import RESET_PENDING, STORAGE_VERSION, DynamicTariffAuditorCoordinator
from .energy_charts import fetch_energy_charts
from .smard import parse_smard_csv

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]
SERVICE_IMPORT_SMARD = "import_smard_prices"
SERVICE_IMPORT_ENERGY_CHARTS = "import_energy_charts_prices"

type DynamicTariffAuditorConfigEntry = ConfigEntry[DynamicTariffAuditorCoordinator]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Dienste registrieren (einmalig, unabhängig vom Eintrag)."""

    async def _handle_smard(call: ServiceCall) -> None:
        prices = await hass.async_add_executor_job(_read_smard, call.data["file"])
        await _async_write_prices(hass, prices)

    async def _handle_energy_charts(call: ServiceCall) -> None:
        start = date.fromisoformat(call.data["start"])
        end = date.fromisoformat(call.data["end"]) if call.data.get("end") else dt_util.now().date()
        prices = await fetch_energy_charts(async_get_clientsession(hass), start, end)
        await _async_write_prices(hass, prices)

    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_SMARD):
        hass.services.async_register(
            DOMAIN, SERVICE_IMPORT_SMARD, _handle_smard,
            schema=vol.Schema({vol.Required("file"): str}),
        )
    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_ENERGY_CHARTS):
        hass.services.async_register(
            DOMAIN, SERVICE_IMPORT_ENERGY_CHARTS, _handle_energy_charts,
            schema=vol.Schema({vol.Required("start"): str, vol.Optional("end"): str}),
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


def _read_smard(path: str) -> dict[datetime, float]:
    """SMARD-CSV lesen (€/MWh, Stundenmittel)."""
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return parse_smard_csv(text, dt_util.get_default_time_zone())


async def _async_write_prices(hass: HomeAssistant, prices_eur_mwh: dict[datetime, float]) -> None:
    """Stundenpreise (€/MWh) in die Statistik des Preissensors schreiben und neu berechnen."""
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
    if not entries:
        raise vol.Invalid("Kein Eintrag von Dynamic Tariff Auditor geladen")
    if not prices_eur_mwh:
        raise vol.Invalid("Keine Preise zum Importieren")
    entry = entries[0]

    # In die Einheit des Preissensors umrechnen
    unit = entry.data[CONF_PRICE_UNIT]
    factor = PRICE_UNIT_FACTORS["€/MWh"] / PRICE_UNIT_FACTORS[unit]
    metadata = StatisticMetaData(
        source="recorder",
        statistic_id=entry.data[CONF_PRICE],
        name=None,
        unit_of_measurement=unit,
        unit_class=None,
        has_mean=True,
        mean_type=StatisticMeanType.ARITHMETIC,
        has_sum=False,
    )
    stats = [StatisticData(start=hour, mean=value * factor) for hour, value in prices_eur_mwh.items()]
    instance = get_instance(hass)
    await instance.async_add_executor_job(import_statistics, instance, metadata, stats, Statistics)
    await _async_reset_and_reload(hass, entry)
