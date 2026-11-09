"""Config-Flow: dynamischer Tarif, danach beliebig viele statische Zeiträume."""
from __future__ import annotations

from datetime import date
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
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
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ADD_MORE,
    CONF_BASE_FEE,
    CONF_CONSUMPTION,
    CONF_DYN_BASE,
    CONF_DYN_FACTOR,
    CONF_DYN_SURCHARGE,
    CONF_EVAL_START,
    CONF_NAME,
    CONF_PRICE,
    CONF_POWER_UNIT,
    CONF_PRICE_UNIT,
    CONF_STATIC_TARIFFS,
    CONF_TARIFF_NAME,
    CONF_VALID_FROM,
    CONF_VALID_TO,
    CONF_WORK_PRICE_CT,
    DOMAIN,
    POWER_UNITS,
    PRICE_UNITS,
)


def _user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default="Tarifvergleich"): str,
            vol.Required(CONF_CONSUMPTION): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Required(CONF_PRICE): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Required(CONF_POWER_UNIT, default="W"): SelectSelector(
                SelectSelectorConfig(options=POWER_UNITS, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Required(CONF_PRICE_UNIT, default="ct/kWh"): SelectSelector(
                SelectSelectorConfig(options=PRICE_UNITS, mode=SelectSelectorMode.DROPDOWN)
            ),
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
                NumberSelectorConfig(min=0, max=200, step=0.0001,
                                     unit_of_measurement="ct/kWh", mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_ADD_MORE, default=False): BooleanSelector(),
        }
    )


class DynamicTariffAuditorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._general: dict[str, Any] = {}
        self._static: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._general = user_input
            return await self.async_step_static()
        return self.async_show_form(step_id="user", data_schema=_user_schema())

    async def async_step_static(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
