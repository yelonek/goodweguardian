"""Nocny anty-flipflop + zielony zapas: sieć nocą na dom, do sieci tylko PV.

Okno zapadki to godziny zegarowe (jak rezerwa nocna), nie strefa G12 — bez 13–14.
Po ładunku z sieci w 22–5 zakaz eksportu do rana (zapadka). Rozładowanie na dom
(exp=0) jest wolne. Energia kupiona z sieci nie powiększa zielonego zapasu —
poranny/wieczorny zrzut do sieci tylko z SOC pochodzenia PV.
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


def _telemetry_cache(
    slots: list[tuple[str, int]],
    telemetry_rows_by_date: dict[str, list[dict]] | None,
) -> dict[str, list[dict]]:
    cache: dict[str, list[dict]] = {}
    if telemetry_rows_by_date is not None:
        cache.update(telemetry_rows_by_date)
        return cache
    from planner.telemetry import read_telemetry_day

    for d_iso, _h in slots:
        if d_iso not in cache:
            cache[d_iso] = read_telemetry_day(date.fromisoformat(d_iso))
    return cache


def night_grid_stored_kwh(
    now: datetime | None = None,
    *,
    capacity_kwh: float,
    telemetry_rows_by_date: dict[str, list[dict]] | None = None,
) -> float:
    """Ile kWh w magazynie przybyło z sieci w już skończonych godzinach tej nocy."""
    if now is None:
        from planner.inputs import _local_now

        now = _local_now()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    cap = max(0.0, float(capacity_kwh))
    if cap <= 0.0:
        return 0.0
    slots = completed_night_slots_before(now)
    if not slots:
        return 0.0
    cache = _telemetry_cache(slots, telemetry_rows_by_date)
    stored = 0.0
    for d_iso, hour in slots:
        rows = cache.get(d_iso, [])
        if not hour_was_night_grid_charge(rows, hour):
            continue
        delta = _soc_delta_pct_for_hour(rows, hour) or 0.0
        stored += max(0.0, delta) / 100.0 * cap
    return stored


def green_stock_kwh_at_start(
    soc_pct: float,
    capacity_kwh: float,
    *,
    now: datetime | None = None,
    telemetry_rows_by_date: dict[str, list[dict]] | None = None,
    night_grid_stored: float | None = None,
) -> float:
    """Zielony zapas [kWh w magazynie]: SOC minus nocny ładunek z sieci.

    Domyślnie cały SOC jest zielony (nadwyżka PV). Po nocnym zakupie odejmujemy
    to, co weszło z sieci — tego nie wolno już oddać do sieci.
    """
    soc = max(0.0, float(soc_pct) / 100.0 * float(capacity_kwh))
    if night_grid_stored is None:
        night_grid_stored = night_grid_stored_kwh(
            now, capacity_kwh=capacity_kwh, telemetry_rows_by_date=telemetry_rows_by_date
        )
    return max(0.0, soc - max(0.0, float(night_grid_stored)))


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

    cache: dict[str, list[dict]] = _telemetry_cache(slots, telemetry_rows_by_date)

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


def resolve_green0_kwh(green_stock_kwh: float | None, soc0_kwh: float) -> float:
    """None = cały bieżący SOC jest zielony (testy / brak korekty z telemetrii)."""
    cap = max(0.0, float(soc0_kwh))
    if green_stock_kwh is None:
        return cap
    return min(max(0.0, float(green_stock_kwh)), cap)


def green_stock_n_vars(n_hours: int) -> int:
    """green[0..H] + ch_pv[H] + dis_g[H]."""
    return (n_hours + 1) + 2 * n_hours


def green_var_indexers(
    base: int, n_hours: int
) -> tuple[Callable[[int], int], Callable[[int], int], Callable[[int], int]]:
    """Indeksy ``green[h]``, ``ch_pv[h]``, ``dis_g[h]`` od ``base``."""
    n_g = n_hours + 1

    def green_index(h: int) -> int:
        return base + h

    def ch_pv_index(h: int) -> int:
        return base + n_g + h

    def dis_g_index(h: int) -> int:
        return base + n_g + n_hours + h

    return green_index, ch_pv_index, dis_g_index


def add_green_stock_constraints(
    eq_rows: list[np.ndarray],
    eq_rhs: list[float],
    ineq_rows: list[np.ndarray],
    ineq_rhs: list[float],
    *,
    n_vars: int,
    n_hours: int,
    eta1: float,
    green0_kwh: float,
    soc_max_kwh: float,
    soc_index: Callable[[int], int],
    ch_index: Callable[[int], int],
    dis_index: Callable[[int], int],
    exp_index: Callable[[int], int] | None,
    green_index: Callable[[int], int],
    ch_pv_index: Callable[[int], int],
    dis_g_index: Callable[[int], int],
    pv_kwh_of: Callable[[int], float],
    pmax_of: Callable[[int], float],
    lb: np.ndarray,
    ub: np.ndarray,
    export_already_kwh_of: Callable[[int], float] | None = None,
) -> None:
    """Eksport z baterii tylko z zielonego zapasu (PV). Nocny zakup z sieci nie powiększa green.

    ``green[h+1] = green[h] + η₁·ch_pv − dis_g/η₁``;
    ``ch_pv ≤ min(ch, pv_rem)``; ``dis_g ≤ dis``;
    ``exp ≤ pv_rem + N₀⁺ + dis_g`` (``N₀⁺`` = już sprzedane w tej h — nie zjadają
    limitu mocy reszty godziny); ``green ≤ soc``.
    """
    n_h = n_hours
    g0 = min(max(0.0, float(green0_kwh)), float(soc_max_kwh))
    row = np.zeros(n_vars)
    row[green_index(0)] = 1.0
    eq_rows.append(row)
    eq_rhs.append(g0)

    for h in range(n_h + 1):
        lb[green_index(h)] = 0.0
        ub[green_index(h)] = soc_max_kwh

    inv_eta = 1.0 / eta1 if eta1 > 0.0 else 1.0
    for h in range(n_h):
        pmax = max(float(pmax_of(h)), 1e-6)
        pv = max(0.0, float(pv_kwh_of(h)))
        already = (
            0.0
            if export_already_kwh_of is None
            else max(0.0, float(export_already_kwh_of(h)))
        )
        lb[ch_pv_index(h)] = 0.0
        ub[ch_pv_index(h)] = min(pmax, pv) if pv > 0.0 else 0.0
        lb[dis_g_index(h)] = 0.0
        ub[dis_g_index(h)] = pmax

        row = np.zeros(n_vars)
        row[green_index(h + 1)] = 1.0
        row[green_index(h)] = -1.0
        row[ch_pv_index(h)] = -eta1
        row[dis_g_index(h)] = inv_eta
        eq_rows.append(row)
        eq_rhs.append(0.0)

        row = np.zeros(n_vars)
        row[ch_pv_index(h)] = 1.0
        row[ch_index(h)] = -1.0
        ineq_rows.append(row)
        ineq_rhs.append(0.0)

        row = np.zeros(n_vars)
        row[dis_g_index(h)] = 1.0
        row[dis_index(h)] = -1.0
        ineq_rows.append(row)
        ineq_rhs.append(0.0)

        if exp_index is not None:
            row = np.zeros(n_vars)
            row[exp_index(h)] = 1.0
            row[dis_g_index(h)] = -1.0
            ineq_rows.append(row)
            ineq_rhs.append(pv + already)

        row = np.zeros(n_vars)
        row[green_index(h)] = 1.0
        row[soc_index(h)] = -1.0
        ineq_rows.append(row)
        ineq_rhs.append(0.0)

    row = np.zeros(n_vars)
    row[green_index(n_h)] = 1.0
    row[soc_index(n_h)] = -1.0
    ineq_rows.append(row)
    ineq_rhs.append(0.0)
