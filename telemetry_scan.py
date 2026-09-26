"""Jeden przebieg pliku telemetry_YYYY-MM-DD.jsonl i cache po mtime.

Agregaty używane przez prognozę load, EV, PV, KPI i historię. Drugie
odczytanie tego samego pliku (dopóki się nie zmieni) nie parsuje JSONL.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()
# path -> (mtime_ns, size, scan)
_cache: dict[str, tuple[int, int, DayScan]] = {}


def clear_telemetry_scan_cache() -> None:
    with _lock:
        _cache.clear()


@dataclass
class DayScan:
    n_rows: int = 0
    load_avg_kwh: dict[int, float] = field(default_factory=dict)
    actual_n: dict[int, int] = field(default_factory=dict)
    actual_cons_sum: dict[int, float] = field(default_factory=dict)
    actual_pv_sum: dict[int, float] = field(default_factory=dict)
    actual_last_remaining: dict[int, float] = field(default_factory=dict)
    actual_last_soc: dict[int, float] = field(default_factory=dict)
    first_pv: dict[int, float] = field(default_factory=dict)
    first_twc: dict[int, float] = field(default_factory=dict)
    max_delta_twc: dict[int, float] = field(default_factory=dict)
    twc_hours: set[int] = field(default_factory=set)
    # (local_date, hour) -> (epoch, seq, E_imp, E_exp); brak licznika = None
    first_counter: dict[tuple[str, int], tuple[float, int, float | None, float | None]] = field(
        default_factory=dict
    )


def _ts_epoch(ts: object) -> float:
    if not isinstance(ts, str) or not ts:
        return float("-inf")
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.timestamp()


def _opt_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scan_file(path: Path) -> DayScan:
    scan = DayScan()
    load_sum: dict[int, float] = {}
    load_n: dict[int, int] = {}
    first_pv_min: dict[int, int] = {}
    first_twc_min: dict[int, int] = {}
    last_key: dict[int, tuple[int, str]] = {}
    seq = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                scan.n_rows += 1
                seq += 1
                try:
                    hour = int(row["local_hour"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not 0 <= hour <= 23:
                    continue

                try:
                    load_w = float(row["consumption_w"])
                except (KeyError, TypeError, ValueError):
                    load_w = None
                if load_w is not None:
                    load_sum[hour] = load_sum.get(hour, 0.0) + load_w
                    load_n[hour] = load_n.get(hour, 0) + 1

                cons = row.get("consumption_w", 0.0)
                pv_w = row.get("pv_w", 0.0)
                try:
                    cons_f = float(cons) if cons is not None else 0.0
                except (TypeError, ValueError):
                    cons_f = 0.0
                try:
                    pv_f = float(pv_w) if pv_w is not None else 0.0
                except (TypeError, ValueError):
                    pv_f = 0.0
                scan.actual_n[hour] = scan.actual_n.get(hour, 0) + 1
                scan.actual_cons_sum[hour] = scan.actual_cons_sum.get(hour, 0.0) + cons_f
                scan.actual_pv_sum[hour] = scan.actual_pv_sum.get(hour, 0.0) + pv_f

                try:
                    minute_loose = int(row.get("local_minute", 0) or 0)
                except (TypeError, ValueError):
                    minute_loose = 0
                ts = row.get("ts_utc", "")
                ts_s = ts if isinstance(ts, str) else ""
                key = (minute_loose, ts_s)
                prev_last = last_key.get(hour)
                if prev_last is None or key > prev_last:
                    last_key[hour] = key
                    rem = row.get("remaining_kwh", 0.0)
                    soc = row.get("soc_pct", 0.0)
                    try:
                        scan.actual_last_remaining[hour] = float(rem if rem is not None else 0.0)
                    except (TypeError, ValueError):
                        scan.actual_last_remaining[hour] = 0.0
                    try:
                        scan.actual_last_soc[hour] = float(soc if soc is not None else 0.0)
                    except (TypeError, ValueError):
                        scan.actual_last_soc[hour] = 0.0

                try:
                    minute = int(row["local_minute"])
                except (KeyError, TypeError, ValueError):
                    minute = None
                if minute is not None:
                    pv_val = row.get("E_pv_kwh")
                    if pv_val is not None:
                        pv_kwh = _opt_float(pv_val)
                        if pv_kwh is not None:
                            prev_m = first_pv_min.get(hour)
                            if prev_m is None or minute < prev_m:
                                first_pv_min[hour] = minute
                                scan.first_pv[hour] = pv_kwh
                    twc_val = row.get("E_twc_kwh")
                    if twc_val is not None:
                        twc_kwh = _opt_float(twc_val)
                        if twc_kwh is not None:
                            prev_t = first_twc_min.get(hour)
                            if prev_t is None or minute < prev_t:
                                first_twc_min[hour] = minute
                                scan.first_twc[hour] = twc_kwh

                if row.get("E_twc_kwh") is not None or row.get("delta_twc_kwh") is not None:
                    scan.twc_hours.add(hour)
                delta_twc = row.get("delta_twc_kwh")
                if delta_twc is not None:
                    dtwc = _opt_float(delta_twc)
                    if dtwc is not None:
                        dtwc = max(0.0, dtwc)
                        prev_d = scan.max_delta_twc.get(hour)
                        if prev_d is None or dtwc > prev_d:
                            scan.max_delta_twc[hour] = dtwc

                local_date = row.get("local_date")
                if isinstance(local_date, str) and local_date:
                    epoch = _ts_epoch(row.get("ts_utc"))
                    cand = (
                        epoch,
                        seq,
                        _opt_float(row.get("E_imp_kwh")),
                        _opt_float(row.get("E_exp_kwh")),
                    )
                    prev_c = scan.first_counter.get((local_date, hour))
                    if prev_c is None or (cand[0], cand[1]) < (prev_c[0], prev_c[1]):
                        scan.first_counter[(local_date, hour)] = cand
    except OSError:
        return DayScan()

    for hour, n in load_n.items():
        if n:
            scan.load_avg_kwh[hour] = (load_sum[hour] / n) / 1000.0
    return scan


def scan_telemetry_file(path: Path) -> DayScan | None:
    """Skan pliku albo None, gdy plik nie istnieje."""
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    key = str(path)
    mtime_ns = st.st_mtime_ns
    size = st.st_size
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == mtime_ns and hit[1] == size:
            return hit[2]
    scan = _scan_file(path)
    with _lock:
        fresh = path.stat() if path.exists() else None
        if fresh is not None and fresh.st_mtime_ns == mtime_ns and fresh.st_size == size:
            _cache[key] = (mtime_ns, size, scan)
    return scan
