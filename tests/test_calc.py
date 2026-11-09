import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/user/custom_components/ha_dynamic_tariff_auditor")

import pytest  # noqa: E402

from calc import (  # noqa: E402
    NO_TARIFF,
    SECONDS_PER_MONTH,
    DynamicTariff,
    Evaluator,
    PriceSeries,
    StaticTariff,
    summarize,
)

TZ = ZoneInfo("Europe/Berlin")


def ts(*args) -> float:
    return datetime(*args, tzinfo=TZ).timestamp()


def make(statics=None, prices=None, dyn=None, start=None, kind="power"):
    if statics is None:
        statics = [
            StaticTariff("Std", date(2025, 1, 1), None, base_fee_month=10.0, work_price=0.30)
        ]
    prices = prices or PriceSeries()
    dyn = dyn or DynamicTariff(base_fee_month=5.0, factor=1.0, surcharge=0.05)
    start = start if start is not None else ts(2025, 1, 1)
    return Evaluator(statics, dyn, prices, TZ, start, kind=kind)


def test_energy_cost_static_and_dynamic():
    prices = PriceSeries()
    prices.add(ts(2025, 1, 1), 0.10)
    ev = make(prices=prices, start=ts(2025, 1, 1, 0))
    ev.consume(ts(2025, 1, 1, 0), 10.0)  # 10 kW ab 00:00
    ev.consume(ts(2025, 1, 1, 1), 0.0)  # 1 Stunde -> 10 kWh
    t = ev.totals()
    assert t.kwh == pytest.approx(10.0)
    assert t.static_energy == pytest.approx(3.0)
    assert t.dyn_energy == pytest.approx(1.5)  # 10 * (0.10 + 0.05)


def test_tick_accrues_without_new_values():
    prices = PriceSeries()
    prices.add(ts(2025, 1, 1), 0.10)
    ev = make(prices=prices, start=ts(2025, 1, 1))
    ev.consume(ts(2025, 1, 1, 0), 2.0)
    ev.tick(ts(2025, 1, 1, 0, 30))  # 30 min à 2 kW = 1 kWh
    assert ev.totals().kwh == pytest.approx(1.0)


def test_negative_power_is_not_consumption():
    prices = PriceSeries()
    prices.add(ts(2025, 1, 1), 0.10)
    ev = make(prices=prices, start=ts(2025, 1, 1))
    ev.consume(ts(2025, 1, 1, 0), -3.0)  # Einspeisung
    ev.consume(ts(2025, 1, 1, 1), 0.0)
    assert ev.totals().kwh == pytest.approx(0.0)


def test_base_fee_prorated_per_month():
    ev = make(start=ts(2025, 1, 1))
    ev.accrue_base(ts(2025, 2, 1))  # 31 Tage
    t = ev.totals()
    expected_static = 10.0 * 31 * 86400 / SECONDS_PER_MONTH
    assert t.static_base == pytest.approx(expected_static)
    assert t.dyn_base == pytest.approx(5.0 * 31 * 86400 / SECONDS_PER_MONTH)


def test_tariff_switch_between_validity_periods():
    statics = [
        StaticTariff("Alt", date(2025, 1, 1), date(2025, 3, 31), 10.0, 0.30),
        StaticTariff("Neu", date(2025, 4, 1), None, 12.0, 0.25),
    ]
    prices = PriceSeries()
    prices.add(ts(2025, 1, 1), 0.10)
    ev = make(statics=statics, prices=prices, start=ts(2025, 3, 31))
    ev.consume(ts(2025, 3, 31, 12), 1.0)
    ev.consume(ts(2025, 3, 31, 13), 1.0)  # 1 kWh Alt
    ev.consume(ts(2025, 3, 31, 23), 1.0)  # 10 kWh Alt
    ev.consume(ts(2025, 4, 1, 0), 1.0)  # 1 kWh Alt (bis Mitternacht)
    ev.consume(ts(2025, 4, 1, 12), 1.0)
    ev.consume(ts(2025, 4, 1, 13), 0.0)  # 1 kWh Neu
    assert ev.buckets["Alt"].kwh == pytest.approx(12.0)
    assert ev.buckets["Alt"].static_energy == pytest.approx(3.6)
    assert ev.buckets["Neu"].static_energy == pytest.approx(13 * 0.25)  # 00-12h + 12-13h


def test_missing_price_is_counted_not_priced():
    ev = make(start=ts(2025, 1, 1))
    ev.consume(ts(2025, 1, 1, 1), 4.0)
    ev.consume(ts(2025, 1, 1, 2), 0.0)
    t = ev.totals()
    assert t.kwh_no_price == pytest.approx(4.0)
    assert t.dyn_energy == 0.0


def test_consumption_without_tariff_is_flagged():
    ev = make(statics=[], start=ts(2025, 1, 1))
    ev.consume(ts(2025, 1, 1, 1), 2.0)
    ev.consume(ts(2025, 1, 1, 2), 0.0)
    assert ev.buckets[NO_TARIFF].kwh_no_tariff == pytest.approx(2.0)


def test_state_roundtrip_and_summary():
    prices = PriceSeries()
    prices.add(ts(2025, 1, 1), 0.10)
    ev = make(prices=prices, start=ts(2025, 1, 1))
    ev.consume(ts(2025, 1, 1, 1), 5.0)
    ev.consume(ts(2025, 1, 1, 2), 0.0)
    state = ev.dump_state()
    ev2 = make(prices=prices, start=ts(2025, 1, 1))
    ev2.load_state(state)
    assert ev2.totals().kwh == pytest.approx(5.0)
    s = summarize(ev2.totals())
    assert s["difference"] == pytest.approx(s["static_cost"] - s["dyn_cost"])
    assert s["avg_static_ct"] is not None


def test_energy_meter_delta_and_reset():
    prices = PriceSeries()
    prices.add(ts(2025, 1, 1), 0.10)
    ev = make(prices=prices, start=ts(2025, 1, 1), kind="energy")
    ev.consume(ts(2025, 1, 1, 0), 100.0)  # Basis
    ev.consume(ts(2025, 1, 1, 1), 103.5)  # +3.5 kWh
    ev.consume(ts(2025, 1, 1, 2), 2.0)  # Reset -> 2 kWh neu
    assert ev.totals().kwh == pytest.approx(5.5)


def test_missing_price_excluded_from_both_sides():
    ev = make(start=ts(2025, 1, 1))  # keine Preise vorhanden
    ev.consume(ts(2025, 1, 1, 1), 4.0)
    ev.consume(ts(2025, 1, 1, 2), 0.0)
    t = ev.totals()
    assert t.kwh == pytest.approx(4.0)
    assert t.static_energy == 0.0
    assert t.dyn_energy == 0.0


def test_month_summary_uses_full_base_fee():
    from calc import month_summary

    prices = PriceSeries()
    prices.add(ts(2025, 1, 1), 0.10)
    ev = make(prices=prices, start=ts(2025, 1, 1))
    ev.consume(ts(2025, 1, 1, 0), 1.0)
    ev.consume(ts(2025, 1, 1, 1), 0.0)  # 1 kWh im Januar
    s = month_summary(ev, ts(2025, 1, 15))
    assert s["month_kwh"] == pytest.approx(1.0)
    assert s["month_static_cost"] == pytest.approx(10.0 + 0.30)  # volle Grundgebühr
    assert s["month_dyn_cost"] == pytest.approx(5.0 + 0.15)


def test_average_prices_are_energy_only_and_priced_only():
    prices = PriceSeries()
    prices.add(ts(2025, 1, 1), 0.10)
    ev = make(prices=prices, start=ts(2025, 1, 1))
    ev.consume(ts(2025, 1, 1, 0), 2.0)
    ev.consume(ts(2025, 1, 1, 1), 0.0)  # 2 kWh mit Preis
    s = summarize(ev.totals())
    assert s["avg_static_ct"] == pytest.approx(30.0)  # 0,30 €/kWh
    assert s["avg_dyn_ct"] == pytest.approx(15.0)  # 0,10 + 0,05 €/kWh


def test_base_fee_split_across_months():
    from calc import yearly

    ev = make(start=ts(2025, 1, 16))
    ev.accrue_base(ts(2025, 2, 16))
    jan = ev.months["2025-01"].static_base
    feb = ev.months["2025-02"].static_base
    assert jan == pytest.approx(10.0 * 16 * 86400 / SECONDS_PER_MONTH)  # 16.–31. Januar
    assert feb == pytest.approx(10.0 * 15 * 86400 / SECONDS_PER_MONTH)  # 1.–15. Februar
    assert yearly(ev.months)["2025"].static_base == pytest.approx(jan + feb)


def test_smard_parser_hourly_mean_and_missing_values():
    import sys
    sys.path.insert(0, "/home/user/custom_components/ha_dynamic_tariff_auditor")
    from smard import parse_smard_csv

    csv = (
        "Datum;Uhrzeit;Deutschland/Luxemburg[€/MWh] Originalauflösungen\n"
        "09.11.2026;00:00;1.234,50\n"
        "09.11.2026;00:15;-\n"
        "09.11.2026;00:30;100,00\n"
        "09.11.2026;01:00;50,00\n"
    )
    res = parse_smard_csv(csv, TZ)
    hours = list(res.values())
    assert hours[0] == pytest.approx((1234.5 + 100.0) / 2)  # Fehlwert ausgelassen
    assert hours[1] == pytest.approx(50.0)
    assert len(res) == 2


def test_end_date_stops_counting():
    prices = PriceSeries()
    prices.add(ts(2025, 1, 1), 0.10)
    ev2 = Evaluator(
        [StaticTariff("Std", date(2025, 1, 1), None, 0.0, 0.30)],
        DynamicTariff(base_fee_month=0.0), prices, TZ, ts(2025, 1, 1),
        kind="power", end_ts=ts(2025, 1, 2),
    )
    ev2.consume(ts(2025, 1, 1, 12), 1.0)
    ev2.consume(ts(2025, 1, 1, 23), 0.0)  # 11 kWh bis 23 Uhr
    ev2.consume(ts(2025, 1, 3, 0), 0.0)  # nach Ende: ignoriert
    ev2.tick(ts(2025, 1, 5))
    assert ev2.totals().kwh == pytest.approx(11.0)


def test_energy_charts_parser_hourly_mean():
    import sys
    sys.path.insert(0, "/home/user/custom_components/ha_dynamic_tariff_auditor")
    from energy_charts import parse_energy_charts

    payload = {"data": [
        {"timestamp": "2026-11-01T00:00:00+01:00", "values": {"day_ahead_price": 100.0}},
        {"timestamp": "2026-11-01T00:15:00+01:00", "values": {"day_ahead_price": 200.0}},
        {"timestamp": "2026-11-01T01:00:00+01:00", "values": {"day_ahead_price": 50.0}},
    ]}
    res = parse_energy_charts(payload)
    values = list(res.values())
    assert values[0] == pytest.approx(150.0)
    assert values[1] == pytest.approx(50.0)
