"""Import von Börsenpreisen aus einer SMARD-CSV (Großhandelspreise DE/LU, €/MWh).

Format (Stand Annahme, bitte mit einem echten Download prüfen):
- Trennzeichen ";"
- Spalten "Datum" (TT.MM.JJJJ) und "Uhrzeit" (HH:MM), eine Spalte mit "€/MWh" im Namen
- Dezimalkomma, Fehlwerte als "-" oder leer
Viertelstundenwerte werden zu Stundenmitteln zusammengefasst.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo


def _parse_value(raw: str) -> float | None:
    raw = raw.strip().replace(".", "").replace(",", ".") if raw.strip() not in ("", "-") else ""
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_smard_csv(text: str, tz: ZoneInfo) -> dict[datetime, float]:
    """Liefert {Stundenbeginn (tz-aware): Preis in €/MWh}."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Leere Datei")
    delimiter = ";" if lines[0].count(";") >= lines[0].count(",") else ","
    header = [h.strip() for h in lines[0].split(delimiter)]

    def find(pred) -> int:
        for i, h in enumerate(header):
            if pred(h):
                return i
        return -1

    i_date = find(lambda h: h.lower().startswith("datum"))
    i_time = find(lambda h: h.lower().startswith("uhrzeit"))
    i_price = find(lambda h: "€/mwh" in h.lower())
    if min(i_date, i_time, i_price) < 0:
        raise ValueError(f"Spalten nicht gefunden (Kopfzeile: {header})")

    buckets: dict[datetime, list[float]] = defaultdict(list)
    for line in lines[1:]:
        cols = line.split(delimiter)
        if len(cols) <= max(i_date, i_time, i_price):
            continue
        value = _parse_value(cols[i_price])
        if value is None:
            continue
        try:
            start = datetime.strptime(f"{cols[i_date].strip()} {cols[i_time].strip()}", "%d.%m.%Y %H:%M")
        except ValueError:
            continue
        hour = start.replace(minute=0, second=0, microsecond=0, tzinfo=tz)
        buckets[hour].append(value)

    return {hour: sum(vals) / len(vals) for hour, vals in sorted(buckets.items())}
