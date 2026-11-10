"""Config-Flow und Optionen: dynamischer Tarif, Tarifbibliothek und Zuweisungen."""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .calc import ENERGY_UNIT_FACTORS, POWER_UNIT_FACTORS, PRICE_UNIT_FACTORS
from .const import (
    CONF_ADD_MORE,
    CONF_CONSUMPTION,
    CONF_CONSUMPTION_FACTOR,
    CONF_CONSUMPTION_KIND,
    CONF_DYN_BASE,
    CONF_DYN_FACTOR,
    CONF_DYN_SURCHARGE,
    CONF_EVAL_END,
    CONF_EVAL_START,
    CONF_NAME,
    CONF_PRICE,
    CONF_PRICE_UNIT,
    CONF_STATIC_TARIFFS,
    CONF_VALID_FROM,
    CONF_VALID_TO,
    DOMAIN,
    VERSION,
)
from .coordinator import RESET_PENDING, STORAGE_VERSION
from .tariff_library import (
    CONF_TARIFF_ID,
    Library,
    assignment_label,
    async_load_library,
    async_save_library,
    new_tariff_id,
    periods_overlap,
    tariffs_in_use,
)

_LOGGER = logging.getLogger(__name__)

CONF_NEW_NAME = "new_name"
CONF_NEW_BASE_FEE = "new_base_fee"
CONF_NEW_WORK_PRICE_CT = "new_work_price_ct"


def _entity_unit(hass: HomeAssistant, entity_id: str) -> str | None:
    """Einheit direkt aus dem Sensor lesen (z. B. W, kW, ct/kWh)."""
    state = hass.states.get(entity_id)
    return state.attributes.get("unit_of_measurement") if state else None


def _user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default="Tarifvergleich"): str,
            vol.Required(CONF_CONSUMPTION): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Required(CONF_PRICE): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Required(CONF_DYN_BASE, default=0.0): NumberSelector(
                NumberSelectorConfig(min=0, max=1000, step=0.01,
                                     unit_of_measurement="€/Monat", mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_DYN_FACTOR, default=1.19): NumberSelector(
                NumberSelectorConfig(min=0, max=10, step=0.001, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_DYN_SURCHARGE, default=18.0): NumberSelector(
                NumberSelectorConfig(min=-100, max=100, step=0.01,
                                     unit_of_measurement="ct/kWh", mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_EVAL_START, default=dt_util.now().date().isoformat()): DateSelector(),
            vol.Optional(CONF_EVAL_END): DateSelector(),
        }
    )


def _dynamic_schema(d: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_DYN_BASE, default=d[CONF_DYN_BASE]): NumberSelector(
                NumberSelectorConfig(min=0, max=1000, step=0.01,
                                     unit_of_measurement="€/Monat", mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_DYN_FACTOR, default=d[CONF_DYN_FACTOR]): NumberSelector(
                NumberSelectorConfig(min=0, max=10, step=0.001, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_DYN_SURCHARGE, default=d[CONF_DYN_SURCHARGE]): NumberSelector(
                NumberSelectorConfig(min=-100, max=100, step=0.01,
                                     unit_of_measurement="ct/kWh", mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_EVAL_START, default=d[CONF_EVAL_START]): DateSelector(),
            vol.Optional(CONF_EVAL_END): DateSelector(),
        }
    )


def _tariff_fields(library: Library, *, add_more: bool) -> dict:
    """Felder zum Zuweisen: vorhandenen Tarif wählen ODER neuen anlegen, dazu Zeitraum."""
    fields: dict = {}
    if library:
        fields[vol.Optional(CONF_TARIFF_ID)] = SelectSelector(
            SelectSelectorConfig(
                options=[{"value": tid, "label": t["name"]} for tid, t in library.items()],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
    fields[vol.Optional(CONF_NEW_NAME)] = str
    fields[vol.Optional(CONF_NEW_BASE_FEE, default=0.0)] = NumberSelector(
        NumberSelectorConfig(min=0, max=1000, step=0.01,
                             unit_of_measurement="€/Monat", mode=NumberSelectorMode.BOX)
    )
    fields[vol.Optional(CONF_NEW_WORK_PRICE_CT)] = NumberSelector(
        NumberSelectorConfig(min=0, max=200, step=0.01,
                             unit_of_measurement="ct/kWh", mode=NumberSelectorMode.BOX)
    )
    fields[vol.Required(CONF_VALID_FROM)] = DateSelector()
    fields[vol.Optional(CONF_VALID_TO)] = DateSelector()
    if add_more:
        fields[vol.Required(CONF_ADD_MORE, default=False)] = BooleanSelector()
    return fields


def _resolve_tariff(user_input: dict[str, Any], library: Library) -> tuple[str | None, Library | None, str | None]:
    """Liefert (tariff_id, geänderte Bibliothek oder None, Fehlercode oder None)."""
    tid = user_input.get(CONF_TARIFF_ID)
    if tid:
        if tid not in library:
            return None, None, "tariff_missing"
        return tid, None, None
    name = (user_input.get(CONF_NEW_NAME) or "").strip()
    work = user_input.get(CONF_NEW_WORK_PRICE_CT)
    if not name or work is None:
        return None, None, "tariff_missing"
    new_id = new_tariff_id()
    updated = {
        **library,
        new_id: {
            "name": name,
            "base_fee": float(user_input.get(CONF_NEW_BASE_FEE) or 0.0),
            "work_price_ct": float(work),
        },
    }
    return new_id, updated, None


def _check_period(valid_from: str, valid_to: str | None, existing: list[dict[str, Any]]) -> str | None:
    if valid_to and date.fromisoformat(valid_to) < date.fromisoformat(valid_from):
        return "invalid_period"
    for a in existing:
        if periods_overlap(valid_from, valid_to, a[CONF_VALID_FROM], a.get(CONF_VALID_TO)):
            return "overlap"
    return None


async def _async_reset_and_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Gespeicherten Stand verwerfen und ab Auswertungsbeginn neu rechnen."""
    RESET_PENDING.add(entry.entry_id)
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    await hass.config_entries.async_reload(entry.entry_id)


def _entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return list(hass.config_entries.async_entries(DOMAIN))


class DynamicTariffAuditorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._general: dict[str, Any] = {}
        self._assignments: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DynamicTariffAuditorOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        try:
            return await self._async_step_user(user_input)
        except Exception:
            _LOGGER.exception("Fehler im Schritt 'user' (Version %s), Eingaben: %s", VERSION, user_input)
            raise

    async def _async_step_user(self, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        if user_input is not None:
            errors: dict[str, str] = {}
            cons_unit = _entity_unit(self.hass, user_input[CONF_CONSUMPTION])
            price_unit = _entity_unit(self.hass, user_input[CONF_PRICE])
            if cons_unit in POWER_UNIT_FACTORS:
                kind, factor = "power", POWER_UNIT_FACTORS[cons_unit]
            elif cons_unit in ENERGY_UNIT_FACTORS:
                kind, factor = "energy", ENERGY_UNIT_FACTORS[cons_unit]
            else:
                kind, factor = None, None
                errors[CONF_CONSUMPTION] = "unsupported_unit"
            if price_unit not in PRICE_UNIT_FACTORS:
                errors[CONF_PRICE] = "unsupported_unit"
            if errors:
                return self.async_show_form(
                    step_id="user", data_schema=_user_schema(), errors=errors,
                    description_placeholders={"version": VERSION},
                )
            self._general = {
                **user_input,
                CONF_CONSUMPTION_KIND: kind,
                CONF_CONSUMPTION_FACTOR: factor,
                CONF_PRICE_UNIT: price_unit,
            }
            if not self._general.get(CONF_EVAL_END):
                self._general.pop(CONF_EVAL_END, None)
            return await self.async_step_assign()
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(),
            description_placeholders={"version": VERSION},
        )

    async def async_step_assign(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        try:
            return await self._async_step_assign(user_input)
        except Exception:
            _LOGGER.exception("Fehler im Schritt 'assign' (Version %s), Eingaben: %s", VERSION, user_input)
            raise

    async def _async_step_assign(self, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        library = await async_load_library(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            tid, updated, err = _resolve_tariff(user_input, library)
            if err is None:
                vf = user_input[CONF_VALID_FROM]
                vt = user_input.get(CONF_VALID_TO) or None
                err = _check_period(vf, vt, self._assignments)
            if err:
                errors["base"] = err
            else:
                if updated is not None:
                    await async_save_library(self.hass, updated)
                self._assignments.append({CONF_TARIFF_ID: tid, CONF_VALID_FROM: vf, CONF_VALID_TO: vt})
                if user_input.get(CONF_ADD_MORE):
                    return await self.async_step_assign()
                return self.async_create_entry(
                    title=self._general[CONF_NAME],
                    data={**self._general, CONF_STATIC_TARIFFS: self._assignments},
                )
        return self.async_show_form(
            step_id="assign",
            data_schema=vol.Schema(_tariff_fields(library, add_more=True)),
            errors=errors,
            description_placeholders={"nr": str(len(self._assignments) + 1)},
        )


class DynamicTariffAuditorOptionsFlow(OptionsFlow):
    """Tarife verwalten: Bibliothek (anlegen, ändern, löschen) und Zuweisungen zum Eintrag."""

    def __init__(self) -> None:
        self._tid: str | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["show", "dynamic", "assign_add", "assign_remove", "lib_add", "lib_edit", "lib_delete"],
        )

    def _assignments(self) -> list[dict[str, Any]]:
        return list(self.config_entry.data[CONF_STATIC_TARIFFS])

    async def _save_assignments(self, assignments: list[dict[str, Any]]) -> ConfigFlowResult:
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, CONF_STATIC_TARIFFS: assignments}
        )
        return self.async_create_entry(title="", data={})

    async def async_step_show(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Nur Anzeige: dynamischer Tarif, Zuweisungen und Bibliothek."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})
        library = await async_load_library(self.hass)
        d = self.config_entry.data
        lines = [
            f"**Dynamischer Tarif:** Grundpreis {d[CONF_DYN_BASE]:.2f} €/Monat, "
            f"Faktor {d[CONF_DYN_FACTOR]}, Aufschlag {d[CONF_DYN_SURCHARGE]:.2f} ct/kWh, "
            f"Auswertung ab {d[CONF_EVAL_START]}, bis {d.get(CONF_EVAL_END) or 'offen'}",
            "",
            "**Zugewiesene statische Tarife:**",
        ]
        for a in self._assignments():
            lines.append(f"- {assignment_label(a, library)}")
        if not self._assignments():
            lines.append("- keine")
        lines += ["", "**Tarifbibliothek:**"]
        for t in library.values():
            lines.append(f"- {t['name']}: Grundpreis {float(t['base_fee']):.2f} €/Monat, "
                         f"Arbeitspreis {float(t['work_price_ct']):.2f} ct/kWh")
        if not library:
            lines.append("- leer")
        return self.async_show_form(
            step_id="show",
            data_schema=vol.Schema({}),
            description_placeholders={"werte": "\n".join(lines)},
        )

    async def async_step_dynamic(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, **user_input, CONF_DYN_BASE: float(user_input[CONF_DYN_BASE])},
            )
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="dynamic", data_schema=_dynamic_schema(self.config_entry.data))

    async def async_step_assign_add(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        library = await async_load_library(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            tid, updated, err = _resolve_tariff(user_input, library)
            if err is None:
                vf = user_input[CONF_VALID_FROM]
                vt = user_input.get(CONF_VALID_TO) or None
                err = _check_period(vf, vt, self._assignments())
            if err:
                errors["base"] = err
            else:
                if updated is not None:
                    await async_save_library(self.hass, updated)
                return await self._save_assignments(
                    [*self._assignments(), {CONF_TARIFF_ID: tid, CONF_VALID_FROM: vf, CONF_VALID_TO: vt}]
                )
        return self.async_show_form(
            step_id="assign_add",
            data_schema=vol.Schema(_tariff_fields(library, add_more=False)),
            errors=errors,
        )

    async def async_step_assign_remove(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        library = await async_load_library(self.hass)
        assignments = self._assignments()
        if user_input is not None:
            idx = int(user_input["assignment"])
            return await self._save_assignments([a for i, a in enumerate(assignments) if i != idx])
        options = [{"value": str(i), "label": assignment_label(a, library)} for i, a in enumerate(assignments)]
        return self.async_show_form(
            step_id="assign_remove",
            data_schema=vol.Schema({vol.Required("assignment"): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))}),
        )

    async def async_step_lib_add(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            library = await async_load_library(self.hass)
            name = (user_input.get(CONF_NEW_NAME) or "").strip()
            if not name:
                return self.async_show_form(step_id="lib_add", data_schema=_lib_schema(), errors={"base": "tariff_missing"})
            library[new_tariff_id()] = {
                "name": name,
                "base_fee": float(user_input.get(CONF_NEW_BASE_FEE) or 0.0),
                "work_price_ct": float(user_input[CONF_NEW_WORK_PRICE_CT]),
            }
            await async_save_library(self.hass, library)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="lib_add", data_schema=_lib_schema())

    async def async_step_lib_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        library = await async_load_library(self.hass)
        if user_input is not None:
            self._tid = user_input[CONF_TARIFF_ID]
            return await self.async_step_lib_edit_form()
        return self.async_show_form(
            step_id="lib_edit",
            data_schema=vol.Schema({vol.Required(CONF_TARIFF_ID): SelectSelector(SelectSelectorConfig(
                options=[{"value": tid, "label": t["name"]} for tid, t in library.items()],
                mode=SelectSelectorMode.DROPDOWN))}),
        )

    async def async_step_lib_edit_form(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        library = await async_load_library(self.hass)
        if self._tid not in library:
            return self.async_abort(reason="tariff_missing")
        current = library[self._tid]
        if user_input is not None:
            name = (user_input.get(CONF_NEW_NAME) or "").strip()
            if not name:
                return self.async_show_form(
                    step_id="lib_edit_form", data_schema=_lib_schema(current), errors={"base": "tariff_missing"})
            library[self._tid] = {
                "name": name,
                "base_fee": float(user_input.get(CONF_NEW_BASE_FEE) or 0.0),
                "work_price_ct": float(user_input[CONF_NEW_WORK_PRICE_CT]),
            }
            await async_save_library(self.hass, library)
            # Einträge mit diesem Tarif ab Auswertungsbeginn neu rechnen
            for entry in _entries(self.hass):
                if tariffs_in_use(self._tid, [entry.data.get(CONF_STATIC_TARIFFS, [])]):
                    self.hass.async_create_task(_async_reset_and_reload(self.hass, entry))
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="lib_edit_form",
            data_schema=_lib_schema(current),
        )

    async def async_step_lib_delete(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        library = await async_load_library(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            tid = user_input[CONF_TARIFF_ID]
            if tariffs_in_use(tid, [e.data.get(CONF_STATIC_TARIFFS, []) for e in _entries(self.hass)]):
                errors["base"] = "in_use"
            else:
                library.pop(tid, None)
                await async_save_library(self.hass, library)
                return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="lib_delete",
            data_schema=vol.Schema({vol.Required(CONF_TARIFF_ID): SelectSelector(SelectSelectorConfig(
                options=[{"value": tid, "label": t["name"]} for tid, t in library.items()],
                mode=SelectSelectorMode.DROPDOWN))}),
            errors=errors,
        )


def _lib_schema(current: dict[str, Any] | None = None) -> vol.Schema:
    cur = current or {}
    return vol.Schema({
        vol.Required(CONF_NEW_NAME, default=cur.get("name", "")): str,
        vol.Optional(CONF_NEW_BASE_FEE, default=float(cur.get("base_fee", 0.0))): NumberSelector(
            NumberSelectorConfig(min=0, max=1000, step=0.01,
                                 unit_of_measurement="€/Monat", mode=NumberSelectorMode.BOX)),
        vol.Required(CONF_NEW_WORK_PRICE_CT, default=float(cur.get("work_price_ct", 0.0))): NumberSelector(
            NumberSelectorConfig(min=0, max=200, step=0.01,
                                 unit_of_measurement="ct/kWh", mode=NumberSelectorMode.BOX)),
    })
