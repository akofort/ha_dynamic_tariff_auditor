"""Börsenpreise (Day-ahead, DE-LU) von der Energy-Charts-API holen.

Endpunkt: https://api.energy-charts.info/v2/price?bzn=DE-LU&start=YYYY-MM-DD&end=YYYY-MM-DD
Antwort: data[] mit timestamp (ISO mit Zeitzone) und values.day_ahead_price (EUR/MWh),
Auflösung 15 min. Die Zeitspanne wird in Monatsblöcke aufgeteilt.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

API_URL = "https://api.energy-charts.info/v2/price"
CHUNK_DAYS = 31


def parse_energy_charts(payload: dict[str, Any]) -> dict[datetime, float]:
    """Liefert {Stundenbeginn (mit Zeitzone): Mittel in EUR/MWh}."""
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for row in payload.get("data", []):
        value = (row.get("values") or {}).get("day_ahead_price")
        if value is None:
            continue
        ts = datetime.fromisoformat(row["timestamp"])
        buckets[ts.replace(minute=0, second=0, microsecond=0)].append(float(value))
    return {hour: sum(vals) / len(vals) for hour, vals in sorted(buckets.items())}


def _chunks(start: date, end_excl: date):
    cur = start
    while cur < end_excl:
        nxt = min(cur + timedelta(days=CHUNK_DAYS), end_excl)
        yield cur, nxt
        cur = nxt


async def fetch_energy_charts(session: aiohttp.ClientSession, start: date, end: date) -> dict[datetime, float]:
    """Preise von `start` bis einschließlich `end` (Tage, Ortszeit)."""
    result: dict[datetime, float] = {}
    for c_start, c_end in _chunks(start, end + timedelta(days=1)):
        params = {"bzn": "DE-LU", "start": c_start.isoformat(), "end": (c_end - timedelta(days=1)).isoformat()}
        async with session.get(API_URL, params=params, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                raise ValueError(f"Energy-Charts antwortet mit HTTP {resp.status}")
            payload = await resp.json()
        result.update(parse_energy_charts(payload))
    if not result:
        raise ValueError("Keine Preise für den Zeitraum erhalten")
    return result
