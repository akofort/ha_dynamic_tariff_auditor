"""Reine Berechnungslogik (ohne Home-Assistant-Abhängigkeit, testbar).

Preise intern in €/kWh, Leistungen in kW, Zeiten als Unix-Timestamps (float).
"""
from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, time, timedelta, tzinfo

SECONDS_PER_MONTH = 30.4375 * 86400
NO_TARIFF = "ohne Tarif"

PRICE_UNIT_FACTORS = {"ct/kWh": 0.01, "€/kWh": 1.0, "€/MWh": 0.001}
POWER_UNIT_FACTORS = {"W": 0.001, "kW": 1.0}  # -> kW
ENERGY_UNIT_FACTORS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}  # -> kWh


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
    """Summen je Tarifzeitraum bzw. Monat."""

    kwh: float = 0.0  # Bezug
    kwh_no_tariff: float = 0.0
    kwh_no_price: float = 0.0
    kwh_priced: float = 0.0  # Bezug mit Tarif UND Preis (Basis für Arbeitspreise)
    static_energy: float = 0.0
    static_base: float = 0.0
    dyn_energy: float = 0.0
    dyn_base: float = 0.0
    feed_kwh: float = 0.0  # Einspeisung
    static_credit: float = 0.0  # Einspeisevergütung statisch (€)
    dyn_credit: float = 0.0  # Einspeisung zum Börsenpreis (€)

    @property
    def static_total(self) -> float:
        return self.static_energy + self.static_base - self.static_credit

    @property
    def dyn_total(self) -> float:
        return self.dyn_energy + self.dyn_base - self.dyn_credit


class Integrator:
    """Wandelt Messwerte in Energiemengen (kWh).

    kind="power": Leistung in kW, Energie = Leistung × Zeit (negative Werte zählen nicht).
    kind="energy": kumulativer Zähler in kWh, Energie = Differenz (Reset wird erkannt).
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.last_power: float | None = None
        self.power_ts: float | None = None
        self.last_meter: float | None = None

    def update(self, ts: float, value: float) -> list[tuple[float, float]]:
        """Neuer Messwert ab `ts`. Liefert (Zeitpunkt, kWh)-Segmente."""
        if self.kind == "power":
            segments: list[tuple[float, float]] = []
            if self.last_power is not None and self.power_ts is not None and ts > self.power_ts:
                kwh = max(self.last_power, 0.0) * (ts - self.power_ts) / 3600
                if kwh > 0:
                    segments.append((self.power_ts, kwh))
            self.last_power = value
            self.power_ts = ts
            return segments
        if self.last_meter is None:
            self.last_meter = value
            return []
        delta = value - self.last_meter
        if delta < 0:  # Zählerreset
            delta = value
        self.last_meter = value
        return [(ts, delta)] if delta > 0 else []

    def tick(self, now: float) -> list[tuple[float, float]]:
        if self.kind == "power" and self.last_power is not None:
            return self.update(now, self.last_power)
        return []

    def dump(self) -> dict:
        return {"last_power": self.last_power, "power_ts": self.power_ts, "last_meter": self.last_meter}

    def load(self, data: dict) -> None:
        self.last_power = data["last_power"]
        self.power_ts = data["power_ts"]
        self.last_meter = data["last_meter"]


class Evaluator:
    """Bewertet Bezug (und optional Einspeisung) gegen beide Tarife."""

    def __init__(
        self,
        statics: list[StaticTariff],
        dynamic: DynamicTariff,
        prices: PriceSeries,
        tz: tzinfo,
        start_ts: float,
        kind: str = "power",
        feed_kind: str | None = None,
        feed_static: float = 0.0,
        end_ts: float | None = None,
    ) -> None:
        self.statics = sorted(statics, key=lambda t: t.valid_from)
        self.dynamic = dynamic
        self.prices = prices
        self.tz = tz
        self.kind = kind
        self.feed_kind = feed_kind
        self.feed_static = feed_static  # €/kWh Einspeisevergütung
        self.end_ts = end_ts
        self.last_ts = start_ts  # Stand der Grundgebühren
        self.cons = Integrator(kind)
        self.feed = Integrator(feed_kind) if feed_kind else None
        self.buckets: dict[str, Bucket] = {}
        self.months: dict[str, Bucket] = {}  # "YYYY-MM" -> Summen inkl. anteiliger Grundgebühr

    # --- Hilfsfunktionen -------------------------------------------------
    def tariff_at(self, ts: float) -> StaticTariff | None:
        day = datetime.fromtimestamp(ts, self.tz).date()
        active = [t for t in self.statics if t.active_on(day)]
        return max(active, key=lambda t: t.valid_from) if active else None

    def _bucket(self, name: str) -> Bucket:
        return self.buckets.setdefault(name, Bucket())

    def _clamp(self, ts: float) -> float:
        return min(ts, self.end_ts) if self.end_ts is not None else ts

    def reset_integrators(self) -> None:
        """Nach Backfill aus Statistik: Messwertbasis neu setzen."""
        self.cons = Integrator(self.kind)
        self.feed = Integrator(self.feed_kind) if self.feed_kind else None

    # --- Berechnung ------------------------------------------------------
    def accrue_base(self, until: float) -> None:
        """Grundgebühren anteilig bis `until` verbuchen (in Tagesabschnitten)."""
        until = self._clamp(until)
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
            mb = self.months.setdefault(local_day.strftime("%Y-%m"), Bucket())
            for target in (b, mb):
                if tariff:
                    target.static_base += tariff.base_fee_month * secs / SECONDS_PER_MONTH
                target.dyn_base += self.dynamic.base_fee_month * secs / SECONDS_PER_MONTH
            t = end
        self.last_ts = max(self.last_ts, until)

    def _add(self, ts: float, kwh: float, feed: bool) -> None:
        """Menge auf BEIDEN Seiten verbuchen, nur wenn Tarif und Preis vorliegen.

        Fehlt eines davon, zählt die Menge auf keiner Seite, sonst würde ein fehlender
        Preis (0 €) die Ersparnis verfälschen.
        """
        if self.end_ts is not None and ts > self.end_ts:
            return
        self.accrue_base(ts)
        tariff = self.tariff_at(ts)
        spot = self.prices.price_at(ts)
        name = tariff.name if tariff else NO_TARIFF
        month = datetime.fromtimestamp(ts, self.tz).strftime("%Y-%m")
        for b in (self._bucket(name), self.months.setdefault(month, Bucket())):
            if feed:
                b.feed_kwh += kwh
            else:
                b.kwh += kwh
                if tariff is None:
                    b.kwh_no_tariff += kwh
                if spot is None:
                    b.kwh_no_price += kwh
            if tariff is None or spot is None:
                continue
            if feed:
                b.static_credit += kwh * self.feed_static
                b.dyn_credit += kwh * spot
            else:
                b.kwh_priced += kwh
                b.static_energy += kwh * tariff.work_price
                b.dyn_energy += kwh * self.dynamic.price(spot)

    def add_consumption(self, ts: float, kwh: float) -> None:
        self._add(ts, kwh, feed=False)

    def add_feed(self, ts: float, kwh: float) -> None:
        self._add(ts, kwh, feed=True)

    def consume(self, ts: float, value: float) -> None:
        """Neuer Messwert des Bezugs (kW oder kWh-Zähler)."""
        self.accrue_base(ts)
        for t, kwh in self.cons.update(ts, value):
            self.add_consumption(t, kwh)

    def export(self, ts: float, value: float) -> None:
        """Neuer Messwert der Einspeisung."""
        if self.feed is None:
            return
        self.accrue_base(ts)
        for t, kwh in self.feed.update(ts, value):
            self.add_feed(t, kwh)

    def tick(self, now: float) -> None:
        """Zeitlichen Fortschritt verbuchen, auch ohne neue Sensorwerte."""
        self.accrue_base(now)
        for t, kwh in self.cons.tick(self._clamp(now)):
            self.add_consumption(t, kwh)
        if self.feed is not None:
            for t, kwh in self.feed.tick(self._clamp(now)):
                self.add_feed(t, kwh)

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
            "cons": self.cons.dump(),
            "feed": self.feed.dump() if self.feed else None,
            "buckets": {k: asdict(v) for k, v in self.buckets.items()},
            "months": {k: asdict(v) for k, v in self.months.items()},
        }

    def load_state(self, data: dict) -> None:
        self.last_ts = data["last_ts"]
        self.cons.load(data["cons"])
        if self.feed is not None and data.get("feed"):
            self.feed.load(data["feed"])
        self.buckets = {k: Bucket(**v) for k, v in data["buckets"].items()}
        self.months = {k: Bucket(**v) for k, v in data.get("months", {}).items()}


def summarize(t: Bucket) -> dict:
    static, dyn = t.static_total, t.dyn_total
    diff = static - dyn  # positiv = dynamischer Tarif günstiger
    return {
        "kwh": t.kwh,
        "feed_kwh": t.feed_kwh,
        "static_cost": static,
        "dyn_cost": dyn,
        "difference": diff,
        "savings_pct": diff / static * 100 if static > 0 else None,
        # Arbeitspreis (ohne Grundgebühr), nur über kWh mit Tarif und Preis.
        # Nur aussagekräftig, wenn mindestens die Hälfte der kWh einen Preis hat.
        "avg_static_ct": t.static_energy / t.kwh_priced * 100 if t.kwh_priced >= 0.5 * t.kwh > 0 else None,
        "avg_dyn_ct": t.dyn_energy / t.kwh_priced * 100 if t.kwh_priced >= 0.5 * t.kwh > 0 else None,
        "kwh_priced": t.kwh_priced,
        "kwh_no_tariff": t.kwh_no_tariff,
        "kwh_no_price": t.kwh_no_price,
    }


def month_summary(ev: Evaluator, now_ts: float) -> dict:
    """Laufender Monat: volle Grundgebühr (nicht anteilig) plus Energie des Monats."""
    local = datetime.fromtimestamp(now_ts, ev.tz)
    key = local.strftime("%Y-%m")
    first = datetime(local.year, local.month, 1, tzinfo=ev.tz).timestamp()
    tariff = ev.tariff_at(first)
    m = ev.months.get(key, Bucket())
    static = m.static_energy - m.static_credit + (tariff.base_fee_month if tariff else 0.0)
    dyn = m.dyn_energy - m.dyn_credit + ev.dynamic.base_fee_month
    diff = static - dyn
    return {
        "month_static_cost": static,
        "month_dyn_cost": dyn,
        "month_difference": diff,
        "month_kwh": m.kwh,
    }


def yearly(months: dict[str, Bucket]) -> dict[str, Bucket]:
    """Monatswerte zu Jahren summieren ("YYYY" -> Bucket)."""
    years: dict[str, Bucket] = {}
    for key, m in months.items():
        y = years.setdefault(key[:4], Bucket())
        for f in fields(Bucket):
            setattr(y, f.name, getattr(y, f.name) + getattr(m, f.name))
    return years
