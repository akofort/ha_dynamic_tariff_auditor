"""Config-Flow: dynamischer Tarif, danach beliebig viele statische Zeiträume."""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)
from homeassistant.util import dt as dt_util

from .calc import ENERGY_UNIT_FACTORS, POWER_UNIT_FACTORS
from .const import (
    VERSION,
    CONF_CONSUMPTION_FACTOR,
    CONF_CONSUMPTION_KIND,
    CONF_PRICE_UNIT,
    PRICE_UNITS,
    CONF_ADD_MORE,
    CONF_BASE_FEE,
    CONF_CONSUMPTION,
    CONF_DYN_BASE,
    CONF_DYN_FACTOR,
    CONF_DYN_SURCHARGE,
    CONF_EVAL_START,
    CONF_NAME,
    CONF_PRICE,
    CONF_STATIC_TARIFFS,
    CONF_TARIFF_NAME,
    CONF_VALID_FROM,
    CONF_VALID_TO,
    CONF_WORK_PRICE_CT,
    DOMAIN,
)


_LOGGER = logging.getLogger(__name__)


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
            vol.Required(CONF_DYN_FACTOR, default=1.0): NumberSelector(
                NumberSelectorConfig(min=0, max=10, step=0.001, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_DYN_SURCHARGE, default=0.0): NumberSelector(
                NumberSelectorConfig(min=-100, max=100, step=0.01,
                                     unit_of_measurement="ct/kWh", mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_EVAL_START, default=dt_util.now().date().isoformat()): DateSelector(),
        }
    )


def _static_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_TARIFF_NAME): str,
            vol.Required(CONF_VALID_FROM): DateSelector(),
            vol.Optional(CONF_VALID_TO): DateSelector(),
            vol.Required(CONF_BASE_FEE, default=0.0): NumberSelector(
                NumberSelectorConfig(min=0, max=1000, step=0.01,
                                     unit_of_measurement="€/Monat", mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_WORK_PRICE_CT): NumberSelector(
                NumberSelectorConfig(min=0, max=200, step=0.01,
                                     unit_of_measurement="ct/kWh", mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_ADD_MORE, default=False): BooleanSelector(),
        }
    )


class DynamicTariffAuditorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DynamicTariffAuditorOptionsFlow()

    def __init__(self) -> None:
        self._general: dict[str, Any] = {}
        self._static: list[dict[str, Any]] = []

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
            if price_unit not in PRICE_UNITS:
                errors[CONF_PRICE] = "unsupported_unit"
            if errors:
                return self.async_show_form(step_id="user", data_schema=_user_schema(), errors=errors)
            self._general = {
                **user_input,
                CONF_CONSUMPTION_KIND: kind,
                CONF_CONSUMPTION_FACTOR: factor,
                CONF_PRICE_UNIT: price_unit,
            }
            return await self.async_step_static()
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(),
            description_placeholders={"version": VERSION},
        )

    async def async_step_static(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        try:
            return await self._async_step_static(user_input)
        except Exception:
            _LOGGER.exception("Fehler im Schritt 'static' (Version %s), Eingaben: %s", VERSION, user_input)
            raise

    async def _async_step_static(self, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            valid_from = date.fromisoformat(user_input[CONF_VALID_FROM])
            valid_to_raw = user_input.get(CONF_VALID_TO)
            valid_to = date.fromisoformat(valid_to_raw) if valid_to_raw else None
            if valid_to is not None and valid_to < valid_from:
                errors["base"] = "invalid_period"
            else:
                self._static.append(
                    {
                        CONF_TARIFF_NAME: user_input[CONF_TARIFF_NAME],
                        CONF_VALID_FROM: valid_from.isoformat(),
                        CONF_VALID_TO: valid_to.isoformat() if valid_to else None,
                        CONF_BASE_FEE: float(user_input[CONF_BASE_FEE]),
                        CONF_WORK_PRICE_CT: float(user_input[CONF_WORK_PRICE_CT]),
                    }
                )
                if user_input[CONF_ADD_MORE]:
                    return await self.async_step_static()
                return self.async_create_entry(
                    title=self._general[CONF_NAME],
                    data={**self._general, CONF_STATIC_TARIFFS: self._static},
                )
        return self.async_show_form(
            step_id="static",
            data_schema=_static_schema(),
            errors=errors,
            description_placeholders={"nr": str(len(self._static) + 1)},
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
        }
    )


def _single_static_schema() -> vol.Schema:
    schema = _static_schema().schema
    return vol.Schema({k: v for k, v in schema.items() if k.schema != CONF_ADD_MORE})


class DynamicTariffAuditorOptionsFlow(OptionsFlow):
    """Tarife nachträglich ändern. Nach dem Speichern wird ab Auswertungsbeginn neu gerechnet."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["dynamic", "static_add", "static_remove"])

    def _save(self, data: dict[str, Any]) -> ConfigFlowResult:
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        return self.async_create_entry(title="", data={})

    async def async_step_dynamic(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save({**self.config_entry.data, **user_input, CONF_DYN_BASE: float(user_input[CONF_DYN_BASE])})
        return self.async_show_form(step_id="dynamic", data_schema=_dynamic_schema(self.config_entry.data))

    async def async_step_static_add(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            valid_from = date.fromisoformat(user_input[CONF_VALID_FROM])
            valid_to_raw = user_input.get(CONF_VALID_TO)
            valid_to = date.fromisoformat(valid_to_raw) if valid_to_raw else None
            if valid_to is not None and valid_to < valid_from:
                errors["base"] = "invalid_period"
            else:
                new = {
                    CONF_TARIFF_NAME: user_input[CONF_TARIFF_NAME],
                    CONF_VALID_FROM: valid_from.isoformat(),
                    CONF_VALID_TO: valid_to.isoformat() if valid_to else None,
                    CONF_BASE_FEE: float(user_input[CONF_BASE_FEE]),
                    CONF_WORK_PRICE_CT: float(user_input[CONF_WORK_PRICE_CT]),
                }
                tariffs = [*self.config_entry.data[CONF_STATIC_TARIFFS], new]
                return self._save({**self.config_entry.data, CONF_STATIC_TARIFFS: tariffs})
        return self.async_show_form(step_id="static_add", data_schema=_single_static_schema(), errors=errors)

    async def async_step_static_remove(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        tariffs = self.config_entry.data[CONF_STATIC_TARIFFS]
        if user_input is not None:
            keep = [t for t in tariffs if t[CONF_TARIFF_NAME] != user_input["tariff"]]
            return self._save({**self.config_entry.data, CONF_STATIC_TARIFFS: keep})
        names = [t[CONF_TARIFF_NAME] for t in tariffs]
        return self.async_show_form(
            step_id="static_remove",
            data_schema=vol.Schema({vol.Required("tariff"): SelectSelector(
                SelectSelectorConfig(options=names, mode=SelectSelectorMode.DROPDOWN))}),
        )
