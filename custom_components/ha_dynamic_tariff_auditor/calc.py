"""Reine Berechnungslogik (ohne Home-Assistant-Abhängigkeit, testbar).

Preise intern immer in €/kWh, Zeiten als Unix-Timestamps (float).
"""
from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, time, timedelta, tzinfo

SECONDS_PER_MONTH = 30.4375 * 86400
NO_TARIFF = "ohne Tarif"

PRICE_UNIT_FACTORS = {"ct/kWh": 0.01, "€/kWh": 1.0, "€/MWh": 0.001}
POWER_UNIT_FACTORS = {"W": 0.001, "kW": 1.0}  # -> kW


@dataclass(frozen=True)
class StaticTariff:
    name: str
    valid_from: date
    valid_to: date | None  # inklusive, None = offen
    base_fee_month: float  # €/Monat
    work_price: float  # €/kWh

    def active_on(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)


@dataclass(frozen=True)
class DynamicTariff:
    base_fee_month: float  # €/Monat
    factor: float = 1.0  # Faktor auf den Börsenpreis
    surcharge: float = 0.0  # €/kWh, nach dem Faktor addiert

    def price(self, spot: float) -> float:
        return spot * self.factor + self.surcharge


class PriceSeries:
    """Börsenpreise (€/kWh) als Zeitreihe; price_at liefert den zuletzt gültigen Wert."""

    def __init__(self) -> None:
        self._ts: list[float] = []
        self._val: list[float] = []

    def __len__(self) -> int:
        return len(self._ts)

    def add(self, ts: float, value: float) -> None:
        i = bisect.bisect_left(self._ts, ts)
        if i < len(self._ts) and self._ts[i] == ts:
            self._val[i] = value
        else:
            self._ts.insert(i, ts)
            self._val.insert(i, value)

    def price_at(self, ts: float) -> float | None:
        i = bisect.bisect_right(self._ts, ts)
        return self._val[i - 1] if i else None


@dataclass
class Bucket:
    """Summen je Tarifzeitraum."""

    kwh: float = 0.0
    kwh_no_tariff: float = 0.0
    kwh_no_price: float = 0.0
    static_energy: float = 0.0
    static_base: float = 0.0
    dyn_energy: float = 0.0
    dyn_base: float = 0.0

    @property
    def static_total(self) -> float:
        return self.static_energy + self.static_base

    @property
    def dyn_total(self) -> float:
        return self.dyn_energy + self.dyn_base


class Evaluator:
    """Bewertet Verbrauch (Leistung in kW, Integration über die Zeit) gegen beide Tarife."""

    def __init__(
        self,
        statics: list[StaticTariff],
        dynamic: DynamicTariff,
        prices: PriceSeries,
        tz: tzinfo,
        start_ts: float,
    ) -> None:
        self.statics = sorted(statics, key=lambda t: t.valid_from)
        self.dynamic = dynamic
        self.prices = prices
        self.tz = tz
        self.last_ts = start_ts  # Stand der Grundgebühren
        self.last_power: float | None = None  # kW seit power_ts
        self.power_ts: float | None = None
        self.buckets: dict[str, Bucket] = {}

    # --- Hilfsfunktionen -------------------------------------------------
    def tariff_at(self, ts: float) -> StaticTariff | None:
        day = datetime.fromtimestamp(ts, self.tz).date()
        active = [t for t in self.statics if t.active_on(day)]
        return max(active, key=lambda t: t.valid_from) if active else None

    def _bucket(self, name: str) -> Bucket:
        return self.buckets.setdefault(name, Bucket())

    # --- Berechnung ------------------------------------------------------
    def accrue_base(self, until: float) -> None:
        """Grundgebühren anteilig bis `until` verbuchen (in Tagesabschnitten)."""
        t = self.last_ts
        while t < until:
            local_day = datetime.fromtimestamp(t, self.tz).date()
            midnight = datetime.combine(
                local_day + timedelta(days=1), time(0), tzinfo=self.tz
            ).timestamp()
            end = min(until, midnight)
            secs = end - t
            tariff = self.tariff_at(t)
            b = self._bucket(tariff.name if tariff else NO_TARIFF)
            if tariff:
                b.static_base += tariff.base_fee_month * secs / SECONDS_PER_MONTH
            b.dyn_base += self.dynamic.base_fee_month * secs / SECONDS_PER_MONTH
            t = end
        self.last_ts = max(self.last_ts, until)

    def add_consumption(self, ts: float, kwh: float) -> None:
        self.accrue_base(ts)
        tariff = self.tariff_at(ts)
        b = self._bucket(tariff.name if tariff else NO_TARIFF)
        b.kwh += kwh
        if tariff:
            b.static_energy += kwh * tariff.work_price
        else:
            b.kwh_no_tariff += kwh
        spot = self.prices.price_at(ts)
        if spot is None:
            b.kwh_no_price += kwh
        else:
            b.dyn_energy += kwh * self.dynamic.price(spot)

    def update_power(self, ts: float, kw: float) -> None:
        """Neuer Leistungswert (kW) ab `ts`. Der vorherige Wert gilt bis `ts`.

        Negative Leistung (Einspeisung) zählt nicht als Bezug.
        """
        self.accrue_base(ts)
        if self.last_power is not None and self.power_ts is not None and ts > self.power_ts:
            kwh = max(self.last_power, 0.0) * (ts - self.power_ts) / 3600
            if kwh > 0:
                self.add_consumption(self.power_ts, kwh)
        self.last_power = kw
        self.power_ts = ts

    def tick(self, now: float) -> None:
        """Zeitlichen Fortschritt verbuchen, auch ohne neue Sensorwerte."""
        if self.last_power is not None:
            self.update_power(now, self.last_power)
        else:
            self.accrue_base(now)

    # --- Auswertung / Persistenz ----------------------------------------
    def totals(self) -> Bucket:
        total = Bucket()
        for b in self.buckets.values():
            for f in fields(Bucket):
                setattr(total, f.name, getattr(total, f.name) + getattr(b, f.name))
        return total

    def dump_state(self) -> dict:
        return {
            "last_ts": self.last_ts,
            "last_power": self.last_power,
            "power_ts": self.power_ts,
            "buckets": {k: asdict(v) for k, v in self.buckets.items()},
        }

    def load_state(self, data: dict) -> None:
        self.last_ts = data["last_ts"]
        self.last_power = data["last_power"]
        self.power_ts = data["power_ts"]
        self.buckets = {k: Bucket(**v) for k, v in data["buckets"].items()}


def summarize(t: Bucket) -> dict:
    static, dyn = t.static_total, t.dyn_total
    diff = static - dyn  # positiv = dynamischer Tarif günstiger
    return {
        "kwh": t.kwh,
        "static_cost": static,
        "dyn_cost": dyn,
        "difference": diff,
        "savings_pct": diff / static * 100 if static > 0 else None,
        "avg_static_ct": static / t.kwh * 100 if t.kwh > 0 else None,
        "avg_dyn_ct": dyn / t.kwh * 100 if t.kwh > 0 else None,
        "kwh_no_tariff": t.kwh_no_tariff,
        "kwh_no_price": t.kwh_no_price,
    }
