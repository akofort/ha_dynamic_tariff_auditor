"""Koordiniert Historie (Recorder), Live-Updates und Persistenz.

Quelle des Verbrauchs ist ein Leistungssensor (W oder kW).

Historie in zwei Stufen:
1. Erstlauf: Stunden-Statistiken (Tabelle `statistics`, bleibt dauerhaft in der DB)
   ab dem Auswertungsstart bis zur letzten vollen Stunde.
2. Danach (und bei jedem Neustart): Zustandsverlauf (`states`), der nur
   `purge_keep_days` zurückreicht – ausreichend für die Zeit seit dem letzten Stand.
"""
from __future__ import annotations

import logging
import math
import time as _time
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .calc import (
    PRICE_UNIT_FACTORS,
    DynamicTariff,
    Evaluator,
    PriceSeries,
    StaticTariff,
    month_summary,
    summarize,
    yearly,
)
from .const import (
    CONF_BASE_FEE,
    CONF_CONSUMPTION,
    CONF_DYN_BASE,
    CONF_DYN_FACTOR,
    CONF_DYN_SURCHARGE,
    CONF_EVAL_START,
    CONF_PRICE,
    CONF_CONSUMPTION_FACTOR,
    CONF_CONSUMPTION_KIND,
    CONF_PRICE_UNIT,
    CONF_STATIC_TARIFFS,
    CONF_TARIFF_NAME,
    CONF_VALID_FROM,
    CONF_VALID_TO,
    CONF_WORK_PRICE_CT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
TICK = timedelta(minutes=15)
HOUR = 3600.0
# Entries, deren gespeicherter Stand verworfen wird (Tarife geändert): nicht erneut speichern
RESET_PENDING: set[str] = set()


def _to_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) else result


class DynamicTariffAuditorCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Hält die Auswertung; Sensoren lesen self.data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN}_{entry.entry_id}")
        d = entry.data
        self._consumption: str = d[CONF_CONSUMPTION]
        self._price_entity: str = d[CONF_PRICE]
        self._price_factor = PRICE_UNIT_FACTORS[d[CONF_PRICE_UNIT]]
        self._kind: str = d[CONF_CONSUMPTION_KIND]  # "power" (kW) oder "energy" (kWh)
        self._cons_factor: float = float(d[CONF_CONSUMPTION_FACTOR])
        self._tz = dt_util.get_default_time_zone()
        self._eval_start: date = date.fromisoformat(d[CONF_EVAL_START])
        self._statics = [
            StaticTariff(
                name=t[CONF_TARIFF_NAME],
                valid_from=date.fromisoformat(t[CONF_VALID_FROM]),
                valid_to=date.fromisoformat(t[CONF_VALID_TO]) if t.get(CONF_VALID_TO) else None,
                base_fee_month=float(t[CONF_BASE_FEE]),
                work_price=float(t[CONF_WORK_PRICE_CT]) / 100,
            )
            for t in d[CONF_STATIC_TARIFFS]
        ]
        self._dynamic = DynamicTariff(
            base_fee_month=float(d[CONF_DYN_BASE]),
            factor=float(d[CONF_DYN_FACTOR]),
            surcharge=float(d[CONF_DYN_SURCHARGE]) / 100,
        )
        self._prices = PriceSeries()
        self._evaluator: Evaluator | None = None
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._unsubs: list[Any] = []

    @staticmethod
    def _is_current_state(stored: dict[str, Any]) -> bool:
        """Stand enthält alle Felder der aktuellen Version."""
        if "months" not in stored:
            return False
        return all("kwh_priced" in b for b in stored.get("buckets", {}).values())

    # --- Start / Stop ----------------------------------------------------
    async def async_setup(self) -> None:
        start_ts = datetime.combine(self._eval_start, time(0), tzinfo=self._tz).timestamp()
        self._evaluator = Evaluator(self._statics, self._dynamic, self._prices, self._tz, start_ts)
        stored = await self._store.async_load()
        if stored and not self._is_current_state(stored):
            # Stand aus einer älteren Version: fehlende Monats- und Arbeitspreisdaten.
            _LOGGER.info("Gespeicherter Stand ist veraltet, Neuberechnung ab Auswertungsbeginn")
            await self._store.async_remove()
            stored = None
        if stored:
            self._evaluator.load_state(stored)
        else:
            await self._async_backfill_statistics()

        await self._async_backfill_states()

        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self._consumption, self._price_entity], self._handle_state_change
            )
        )
        self._unsubs.append(async_track_time_interval(self.hass, self._async_handle_tick, TICK))
        RESET_PENDING.discard(self.config_entry.entry_id)
        self.async_set_updated_data(self._snapshot())

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._evaluator is not None and self.config_entry.entry_id not in RESET_PENDING:
            self._evaluator.tick(_time.time())
            await self._async_save()

    # --- Stufe 1: Stunden-Statistiken (langfristig in der DB) ----------------
    def _fetch_statistics(self, start: float, end: float) -> dict[str, list[dict]]:
        return statistics_during_period(
            self.hass,
            dt_util.utc_from_timestamp(start),
            dt_util.utc_from_timestamp(end),
            {self._consumption, self._price_entity},
            "hour",
            None,
            {"mean", "change"},
        )

    async def _async_backfill_statistics(self) -> None:
        """Erstlauf: Stundenwerte ab Auswertungsstart bis zur letzten vollen Stunde."""
        assert self._evaluator is not None
        ev = self._evaluator
        start = ev.last_ts
        hour_end = math.floor(_time.time() / HOUR) * HOUR
        if start >= hour_end:
            return
        try:
            rows = await get_instance(self.hass).async_add_executor_job(
                self._fetch_statistics, start, hour_end
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Statistiken konnten nicht aus dem Recorder gelesen werden")
            return

        # Preise je Stunde (Mittelwert) zuerst, damit Verbrauch den Preis findet
        for row in rows.get(self._price_entity, []):
            mean = _to_float(row.get("mean"))
            if mean is not None:
                self._prices.add(row["start"], mean * self._price_factor)
        for row in rows.get(self._consumption, []):
            if self._kind == "power":
                # Mittlere Leistung je Stunde x 1 h = kWh dieser Stunde
                mean = _to_float(row.get("mean"))
                if mean is not None and mean > 0:
                    ev.add_consumption(row["start"], mean * self._cons_factor)
            else:
                # Zähler: Verbrauch der Stunde = Differenz des Zählers
                change = _to_float(row.get("change"))
                if change is not None and change > 0:
                    ev.add_consumption(row["start"], change * self._cons_factor)
        ev.accrue_base(hour_end)
        # Ab hier übernimmt der Zustandsverlauf; die Basis wird neu gesetzt.
        ev.last_power = None
        ev.power_ts = None
        ev.last_meter = None
        await self._async_save()

    # --- Stufe 2: Zustandsverlauf (Recorder-Retention) -----------------------
    def _fetch_states(self, start: float, end: float) -> tuple[list[State], list[State]]:
        start_dt = dt_util.utc_from_timestamp(start)
        end_dt = dt_util.utc_from_timestamp(end)
        prices = history.state_changes_during_period(self.hass, start_dt, end_dt, self._price_entity)
        meters = history.state_changes_during_period(self.hass, start_dt, end_dt, self._consumption)
        return prices.get(self._price_entity, []), meters.get(self._consumption, [])

    async def _async_backfill_states(self) -> None:
        assert self._evaluator is not None
        ev = self._evaluator
        start = ev.last_ts
        now = _time.time()
        if start >= now:
            return
        try:
            price_states, meter_states = await get_instance(self.hass).async_add_executor_job(
                self._fetch_states, start, now
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Zustandsverlauf konnte nicht aus dem Recorder gelesen werden")
            return

        for st in price_states:
            value = _to_float(st.state)
            if value is not None:
                self._prices.add(max(st.last_updated.timestamp(), start), value * self._price_factor)

        events = sorted(
            (max(st.last_updated.timestamp(), start), value * self._cons_factor)
            for st in meter_states
            if (value := _to_float(st.state)) is not None
        )
        for ts, value in events:
            self._apply_consumption(ts, value)
        ev.tick(now)
        await self._async_save()

    # --- Live -------------------------------------------------------------
    @callback
    def _handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is None or self._evaluator is None:
            return
        value = _to_float(new.state)
        if value is None:
            return
        ts = new.last_updated.timestamp()
        if event.data["entity_id"] == self._price_entity:
            self._prices.add(ts, value * self._price_factor)
        else:
            self._apply_consumption(ts, value * self._cons_factor)
        self.async_set_updated_data(self._snapshot())

    def _apply_consumption(self, ts: float, value: float) -> None:
        """Leistung (kW) integrieren bzw. Zählerstand (kWh) verbuchen."""
        assert self._evaluator is not None
        if self._kind == "power":
            self._evaluator.update_power(ts, value)
        else:
            self._evaluator.update_meter(ts, value)

    async def _async_handle_tick(self, now: datetime) -> None:
        if self._evaluator is None:
            return
        self._evaluator.tick(now.timestamp())
        self.async_set_updated_data(self._snapshot())
        await self._async_save()

    # --- Hilfen -------------------------------------------------------------
    async def _async_save(self) -> None:
        if self._evaluator is not None:
            await self._store.async_save(self._evaluator.dump_state())

    def _snapshot(self) -> dict[str, Any]:
        assert self._evaluator is not None
        data = summarize(self._evaluator.totals())
        data["buckets"] = {n: summarize(b) for n, b in self._evaluator.buckets.items()}
        data.update(month_summary(self._evaluator, _time.time()))
        # Eingegebene Werte (aktuell gültiger statischer Tarif), zur Kontrolle
        now_tariff = self._evaluator.tariff_at(_time.time())
        data["cfg_work_static_ct"] = now_tariff.work_price * 100 if now_tariff else None
        data["cfg_base_static"] = now_tariff.base_fee_month if now_tariff else None
        data["cfg_base_dyn"] = self._dynamic.base_fee_month
        data["cfg_surcharge_dyn_ct"] = self._dynamic.surcharge * 100
        months = {k: summarize(b) for k, b in sorted(self._evaluator.months.items())}
        data["monate"] = months
        data["jahre"] = {k: summarize(b) for k, b in sorted(yearly(self._evaluator.months).items())}
        return data
