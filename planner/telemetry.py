"""Odczyt telemetrii do rekonsyliacji i review."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from guardian_config import TELEMETRY_DIR


def read_telemetry_day(local_date: date) -> list[dict]:
    path = TELEMETRY_DIR / f"telemetry_{local_date.isoformat()}.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def hourly_actuals(local_date: date) -> dict[int, dict]:
    """
    Agregaty per godzina z telemetrii minutowej.

    - ``net_kwh``: ostatni ``remaining_kwh`` w godzinie (Δexp−Δimp)
    - ``load_kwh``, ``pv_kwh``: średnia moc / 1000 * liczba próbek/60… uproszczenie: suma energii z przyrostów
    """
    rows = read_telemetry_day(local_date)
    by_hour: dict[int, list[dict]] = {h: [] for h in range(24)}
    for r in rows:
        try:
            h = int(r["local_hour"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= h <= 23:
            by_hour[h].append(r)

    out: dict[int, dict] = {}
    for h in range(24):
        bucket = by_hour[h]
        if not bucket:
            continue
        last = max(bucket, key=lambda x: (int(x.get("local_minute", 0)), x.get("ts_utc", "")))
        net = float(last.get("remaining_kwh", 0.0))
        avg_cons_w = sum(float(x.get("consumption_w", 0.0)) for x in bucket) / len(bucket)
        avg_pv_w = sum(float(x.get("pv_w", 0.0)) for x in bucket) / len(bucket)
        n_min = len(bucket)
        load_kwh = (avg_cons_w / 1000.0) * (n_min / 60.0)
        pv_kwh = (avg_pv_w / 1000.0) * (n_min / 60.0)
        out[h] = {
            "net_kwh": net,
            "load_kwh": load_kwh,
            "pv_kwh": pv_kwh,
            "samples": n_min,
            "last_soc_pct": float(last.get("soc_pct", 0.0)),
        }
    return out
