"""Nocny anty-flipflop: po ładunku z sieci w oknie 22–5 zakaz eksportu do rana.

Okno to godziny zegarowe (jak rezerwa nocna), nie strefa G12 — bez 13–14.
Zapadka resetuje się po luce dziennej (6–21). Rozładowanie na dom (exp=0) jest wolne.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from planner.models import HourInputs, HourPlan

NIGHT_GRID_HOURS: frozenset[int] = frozenset({22, 23, 0, 1, 2, 3, 4, 5})
# Zgodne z NET_NEUTRAL_EPS_KWH — szum poniżej tego nie zapina zapadki.
CHARGE_EPS_KWH = 0.05
# Wzrost SOC w godzinie, który uznajemy za ładunek (carry-in z telemetrii).
SOC_CHARGE_EPS_PCT = 1.0
PV_NEGLIGIBLE_KWH = 0.05


def is_night_grid_hour(hour: int) -> bool:
    return int(hour) in NIGHT_GRID_HOURS


def night_windows(slots: Sequence[Any]) -> list[list[int]]:
    """Indeksy godzin pogrupowane w spójne okna nocy (przez północ = jedno okno)."""
    windows: list[list[int]] = []
    current: list[int] = []
    for i, slot in enumerate(slots):
        hour = int(slot.hour)
        if hour in NIGHT_GRID_HOURS:
            if current and i == current[-1] + 1:
                current.append(i)
            else:
                if current:
                    windows.append(current)
                current = [i]
        elif current:
            windows.append(current)
            current = []
    if current:
        windows.append(current)
    return windows


def night_latch_layout(slots: Sequence[Any]) -> tuple[list[list[int]], dict[int, int]]:
    """Okna + mapa indeks godziny horyzontu → indeks zmiennej y_ch/latch."""
    windows = night_windows(slots)
    pos: dict[int, int] = {}
    k = 0
    for window in windows:
        for h in window:
            pos[h] = k
            k += 1
    return windows, pos


def carry_in_applies_to_window(window: list[int], slots: Sequence[Any]) -> bool:
    """Carry-in tylko do okna, które trwa w chwili startu horyzontu (pierwsza h jest nocą)."""
    if not window or not slots:
        return False
    return window[0] == 0 and int(slots[0].hour) in NIGHT_GRID_HOURS


def completed_night_slots_before(now: datetime) -> list[tuple[str, int]]:
    """Skończone godziny bieżącego okna nocy przed ``now`` (bez bieżącej h)."""
    hour = int(now.hour)
    if hour not in NIGHT_GRID_HOURS:
        return []
    d = now.date() if isinstance(now, datetime) else now
    if hour <= 5:
        prev = d - timedelta(days=1)
        slots = [(prev.isoformat(), 22), (prev.isoformat(), 23)]
        slots.extend((d.isoformat(), h) for h in range(hour))
        return slots
    return [(d.isoformat(), h) for h in range(22, hour)]


def _soc_delta_pct_for_hour(rows: list[dict], hour: int) -> float | None:
    bucket: list[dict] = []
    for row in rows:
        try:
            if int(row.get("local_hour", -1)) != hour:
                continue
        except (TypeError, ValueError):
            continue
        bucket.append(row)
    if len(bucket) < 2:
        return None

    def _key(row: dict) -> tuple[int, str]:
        try:
            minute = int(row.get("local_minute", 0))
        except (TypeError, ValueError):
            minute = 0
        return minute, str(row.get("ts_utc", ""))

    first = min(bucket, key=_key)
    last = max(bucket, key=_key)
    try:
        return float(last["soc_pct"]) - float(first["soc_pct"])
    except (KeyError, TypeError, ValueError):
        return None


def _pv_kwh_for_hour(rows: list[dict], hour: int) -> float:
    bucket = []
    for row in rows:
        try:
            if int(row.get("local_hour", -1)) != hour:
                continue
        except (TypeError, ValueError):
            continue
        bucket.append(row)
    if not bucket:
        return 0.0
    avg_pv_w = sum(float(x.get("pv_w", 0.0) or 0.0) for x in bucket) / len(bucket)
    avg_kw = avg_pv_w / 1000.0
    frac = min(1.0, len(bucket) / 60.0)
    # Pełna godzina: energia = średnia × ułamek; rzadkie próbki: średnia jako kWh/h
    # (inaczej 2 minuty 800 W wyglądają jak znikome PV).
    return avg_kw * frac if frac >= 0.5 else avg_kw


def hour_was_night_grid_charge(rows: list[dict], hour: int) -> bool:
    """SOC wzrósł w godzinie przy znikomym PV → ładunek z sieci."""
    delta = _soc_delta_pct_for_hour(rows, hour)
    if delta is None or delta <= SOC_CHARGE_EPS_PCT:
        return False
    return _pv_kwh_for_hour(rows, hour) <= PV_NEGLIGIBLE_KWH


def night_grid_charge_carry_in(
    now: datetime | None = None,
    *,
    telemetry_rows_by_date: dict[str, list[dict]] | None = None,
) -> bool:
    """Czy w już skończonych godzinach bieżącego okna nocy był ładunek z sieci."""
    if now is None:
        from planner.inputs import _local_now

        now = _local_now()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    slots = completed_night_slots_before(now)
    if not slots:
        return False

    cache: dict[str, list[dict]] = {}
    if telemetry_rows_by_date is not None:
        cache.update(telemetry_rows_by_date)
    else:
        from planner.telemetry import read_telemetry_day

        for d_iso, _h in slots:
            if d_iso not in cache:
                cache[d_iso] = read_telemetry_day(date.fromisoformat(d_iso))

    for d_iso, hour in slots:
        if hour_was_night_grid_charge(cache.get(d_iso, []), hour):
            return True
    return False


def night_export_blocked_from_plans(
    plans: Sequence[HourPlan],
    *,
    carry_in: bool = False,
    charge_eps_kwh: float = CHARGE_EPS_KWH,
) -> frozenset[tuple[str, int]]:
    """Sloty (date, hour) w oknie nocy, w których mapper nie może emitować eksportu."""
    windows = night_windows(plans)
    blocked: set[tuple[str, int]] = set()
    for window in windows:
        latched = bool(carry_in) and carry_in_applies_to_window(window, plans)
        for h in window:
            hp = plans[h]
            if float(hp.battery_delta_kwh) > charge_eps_kwh:
                latched = True
            if latched:
                blocked.add((str(hp.date), int(hp.hour)))
    return frozenset(blocked)


def add_night_latch_constraints(
    ineq_rows: list[np.ndarray],
    ineq_rhs: list[float],
    *,
    n_vars: int,
    hours_in: list[HourInputs],
    ch_index: Callable[[int], int],
    exp_index: Callable[[int], int],
    y_ch_index: Callable[[int], int],
    latch_index: Callable[[int], int],
    pmax_of: Callable[[int], float],
    big_m: float,
    carry_in: bool,
    eps: float = CHARGE_EPS_KWH,
) -> None:
    """Dopisz nierówności zapadki: ch ≤ ε + Pmax·y; latch ≥ y, latch ≥ prev; exp ≤ M·(1−latch)."""
    windows, _pos = night_latch_layout(hours_in)
    for window in windows:
        window_carry = bool(carry_in) and carry_in_applies_to_window(window, hours_in)
        prev_h: int | None = None
        for h in window:
            y_i = y_ch_index(h)
            latch_i = latch_index(h)
            pmax = max(float(pmax_of(h)), 1e-6)

            # ch[h] - Pmax·y_ch[h] ≤ ε
            row = np.zeros(n_vars)
            row[ch_index(h)] = 1.0
            row[y_i] = -pmax
            ineq_rows.append(row)
            ineq_rhs.append(eps)

            # y_ch[h] - latch[h] ≤ 0
            row = np.zeros(n_vars)
            row[y_i] = 1.0
            row[latch_i] = -1.0
            ineq_rows.append(row)
            ineq_rhs.append(0.0)

            if prev_h is not None:
                # latch[prev] - latch[h] ≤ 0
                row = np.zeros(n_vars)
                row[latch_index(prev_h)] = 1.0
                row[latch_i] = -1.0
                ineq_rows.append(row)
                ineq_rhs.append(0.0)
            elif window_carry:
                # 1 - latch[h] ≤ 0  →  latch[first] ≥ 1
                row = np.zeros(n_vars)
                row[latch_i] = -1.0
                ineq_rows.append(row)
                ineq_rhs.append(-1.0)

            # exp[h] + M·latch[h] ≤ M
            row = np.zeros(n_vars)
            row[exp_index(h)] = 1.0
            row[latch_i] = big_m
            ineq_rows.append(row)
            ineq_rhs.append(big_m)

            prev_h = h
