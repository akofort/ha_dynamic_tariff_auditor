"""Tarifbibliothek: benannte statische Tarife, die Einträgen mit eigenem Zeitraum zugewiesen werden.

Die Bibliothek liegt in einer eigenen Speicherdatei (unabhängig von den Einträgen).
Ein Eintrag speichert nur Verweise: {tariff_id, valid_from, valid_to}.
Alte Einträge mit direkt gespeicherten Werten (tariff_name, base_fee, ...) bleiben lesbar.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .calc import StaticTariff, periods_overlap
from .const import (
    CONF_BASE_FEE,
    CONF_TARIFF_NAME,
    CONF_VALID_FROM,
    CONF_VALID_TO,
    CONF_WORK_PRICE_CT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
LIBRARY_STORE_VERSION = 1
LIBRARY_KEY = f"{DOMAIN}.tariffs"
CONF_TARIFF_ID = "tariff_id"

Library = dict[str, dict[str, Any]]  # id -> {"name", "base_fee", "work_price_ct"}


def _store(hass: HomeAssistant) -> Store[dict[str, Any]]:
    return Store(hass, LIBRARY_STORE_VERSION, LIBRARY_KEY)


async def async_load_library(hass: HomeAssistant) -> Library:
    data = await _store(hass).async_load() or {}
    return dict(data.get("tariffs", {}))


async def async_save_library(hass: HomeAssistant, library: Library) -> None:
    await _store(hass).async_save({"tariffs": library})


def new_tariff_id() -> str:
    return uuid.uuid4().hex[:12]


def build_statics(assignments: list[dict[str, Any]], library: Library) -> list[StaticTariff]:
    """Zuweisungen eines Eintrags in Tarife für die Berechnung umwandeln."""
    result: list[StaticTariff] = []
    for a in assignments:
        if CONF_TARIFF_ID in a:
            t = library.get(a[CONF_TARIFF_ID])
            if t is None:
                _LOGGER.error("Tarif %s fehlt in der Tarifbibliothek und wird übersprungen", a[CONF_TARIFF_ID])
                continue
            name, base, work = t["name"], float(t["base_fee"]), float(t["work_price_ct"]) / 100
        else:  # alte Einträge mit direkt gespeicherten Werten
            name = a[CONF_TARIFF_NAME]
            base = float(a[CONF_BASE_FEE])
            work = float(a[CONF_WORK_PRICE_CT]) / 100
        result.append(
            StaticTariff(
                name=name,
                valid_from=date.fromisoformat(a[CONF_VALID_FROM]),
                valid_to=date.fromisoformat(a[CONF_VALID_TO]) if a.get(CONF_VALID_TO) else None,
                base_fee_month=base,
                work_price=work,
            )
        )
    return result


def assignment_label(a: dict[str, Any], library: Library) -> str:
    if CONF_TARIFF_ID in a:
        name = library.get(a[CONF_TARIFF_ID], {}).get("name", "(gelöscht)")
    else:
        name = a[CONF_TARIFF_NAME]
    return f"{name}: {a[CONF_VALID_FROM]} bis {a.get(CONF_VALID_TO) or 'offen'}"


def tariffs_in_use(library_id: str, entries_assignments: list[list[dict[str, Any]]]) -> bool:
    return any(a.get(CONF_TARIFF_ID) == library_id for assigns in entries_assignments for a in assigns)
