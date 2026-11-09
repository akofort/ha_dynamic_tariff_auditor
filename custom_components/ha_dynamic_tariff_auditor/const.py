"""Konstanten für ha_dynamic_tariff_auditor."""

DOMAIN = "ha_dynamic_tariff_auditor"
VERSION = "0.6.0"

CONF_NAME = "name"
CONF_CONSUMPTION = "consumption_entity"
CONF_PRICE = "price_entity"
CONF_CONSUMPTION_KIND = "consumption_kind"  # "power" oder "energy"
CONF_CONSUMPTION_FACTOR = "consumption_factor"  # Umrechnung auf kW bzw. kWh
CONF_PRICE_UNIT = "price_unit"
CONF_DYN_BASE = "dyn_base_fee"
CONF_DYN_FACTOR = "dyn_factor"
CONF_DYN_SURCHARGE = "dyn_surcharge_ct"
CONF_EVAL_START = "eval_start"
CONF_STATIC_TARIFFS = "static_tariffs"
CONF_EVAL_END = "eval_end"  # optional: Auswertung bis

CONF_TARIFF_NAME = "tariff_name"
CONF_VALID_FROM = "valid_from"
CONF_VALID_TO = "valid_to"
CONF_BASE_FEE = "base_fee"
CONF_WORK_PRICE_CT = "work_price_ct"
CONF_ADD_MORE = "add_more"

PRICE_UNITS = ["ct/kWh", "€/kWh", "€/MWh"]
