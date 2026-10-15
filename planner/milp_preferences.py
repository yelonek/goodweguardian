"""Mikro-preferencje MILP: remis dzienny, rozkład ładowania, derating >90% SOC.

Tips są rzędu 1e-5…1e-4 PLN — nie przebijają realnych różnic G12/RCE, tylko
rozstrzygają remisy i lekko wypuklają koszt mocy ładowania.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np

from planner.battery import BatteryParams, soc_kwh
from planner.models import HourInputs
from planner.night_grid_policy import is_night_grid_hour

# Powyżej tego SOC moc ładowania spada (fizyka GoodWe / limity BMS).
CHARGE_DERATE_SOC_PCT = 90.0
CHARGE_DERATE_KW = 2.0

# Dzień, remis G12: wcześniejszy import droższy o ε·(H−h) → kup później.
IMPORT_DEFER_EPS_PLN = 1e-5

# Kara za peak mocy ładowania [PLN / (kWh/h)] — rozkład na wiele godzin.
CHARGE_PEAK_EPS_PLN = 1e-4


def preference_aux_n_vars(n_hours: int) -> int:
    """``t_lo[h]``, ``b_hi[h]`` (bin: SOC≥90%), ``peak``."""
    n_h = int(n_hours)
    return 2 * n_h + 1


def preference_indexers(
    base: int, n_hours: int
) -> tuple[Callable[[int], int], Callable[[int], int], int]:
    """Zwraca ``t_lo_idx``, ``b_hi_idx``, ``peak_idx``."""
    n_h = int(n_hours)

    def t_lo_idx(h: int) -> int:
        return base + h

    def b_hi_idx(h: int) -> int:
        """1 = start godziny w strefie derate (SOC ≥ 90%)."""
        return base + n_h + h

    peak_idx = base + 2 * n_h
    return t_lo_idx, b_hi_idx, peak_idx


def apply_day_import_defer_tips(
    c: np.ndarray,
    hours_in: Sequence[HourInputs],
    *,
    imp_index: Callable[[int], int],
    scale: float = 1.0,
) -> None:
    """Przy remisie G12 w dzień: odsuwaj import na późniejsze sloty horyzontu.

    ``scale`` — waga scenariusza (π) przy E[CF]; deterministycznie 1.
    """
    n_h = len(hours_in)
    eps = float(IMPORT_DEFER_EPS_PLN) * float(scale)
    if eps <= 0.0 or n_h <= 1:
        return
    for h, hin in enumerate(hours_in):
        if is_night_grid_hour(int(hin.hour)):
            continue
        c[imp_index(h)] += eps * float(n_h - h)


def apply_charge_spread_tips(
    c: np.ndarray,
    *,
    peak_idx: int,
) -> None:
    eps_peak = float(CHARGE_PEAK_EPS_PLN)
    if eps_peak > 0.0:
        c[peak_idx] += eps_peak


def add_charge_preference_constraints(
    ineq_rows: list[np.ndarray],
    ineq_rhs: list[float],
    *,
    n_vars: int,
    hours_in: Sequence[HourInputs],
    params: BatteryParams,
    soc_index: Callable[[int], int],
    ch_index: Callable[[int], int],
    t_lo_index: Callable[[int], int],
    b_hi_index: Callable[[int], int],
    peak_idx: int,
    lb: np.ndarray,
    ub: np.ndarray,
) -> None:
    """Derating ładowania powyżej ``CHARGE_DERATE_SOC_PCT`` + peak ≥ ch_h.

    ``b_hi=1`` ⇒ SOC na starcie h ≥ 90% ⇒ tylko moc derate; ``t_lo`` (czas full-rate)
    tylko gdy ``b_hi=0``, ograniczone headroomem do 90%.
    """
    n_h = len(hours_in)
    eta1 = float(params.eta_one_way)
    soc_90 = soc_kwh(min(CHARGE_DERATE_SOC_PCT, params.soc_max_pct), params)
    soc_max = soc_kwh(params.soc_max_pct, params)
    p_full_1h = float(params.max_power_kwh_per_h)
    p_derate = max(0.0, float(CHARGE_DERATE_KW))
    p_d_1h = min(p_full_1h, p_derate)
    # Big-M: SOC i energia full-rate w jednej godzinie.
    m_soc = max(soc_max, soc_90, 1.0) + p_full_1h + 1.0

    for h, hin in enumerate(hours_in):
        frac = float(hin.hour_fraction) if hin.hour_fraction > 0 else 1.0
        frac = min(1.0, max(1e-6, frac))

        lb[t_lo_index(h)] = 0.0
        ub[t_lo_index(h)] = frac
        lb[b_hi_index(h)] = 0.0
        ub[b_hi_index(h)] = 1.0

        # soc <= soc_90 + M·b_hi
        row = np.zeros(n_vars)
        row[soc_index(h)] = 1.0
        row[b_hi_index(h)] = -m_soc
        ineq_rows.append(row)
        ineq_rhs.append(soc_90)

        # soc >= soc_90 − M·(1−b_hi)  ⇔  −soc + M·b_hi <= M − soc_90
        row = np.zeros(n_vars)
        row[soc_index(h)] = -1.0
        row[b_hi_index(h)] = m_soc
        ineq_rows.append(row)
        ineq_rhs.append(m_soc - soc_90)

        # t_lo <= frac·(1−b_hi)
        row = np.zeros(n_vars)
        row[t_lo_index(h)] = 1.0
        row[b_hi_index(h)] = frac
        ineq_rows.append(row)
        ineq_rhs.append(frac)

        # η·P_full·t_lo <= soc_90 − soc + M·b_hi
        # (gdy b_hi=0: headroom do 90%; gdy b_hi=1: t_lo=0 i tak)
        row = np.zeros(n_vars)
        row[t_lo_index(h)] = eta1 * p_full_1h
        row[soc_index(h)] = 1.0
        row[b_hi_index(h)] = -m_soc
        ineq_rows.append(row)
        ineq_rhs.append(soc_90)

        # ch <= P_d·frac + (P_full − P_d)·t_lo
        row = np.zeros(n_vars)
        row[ch_index(h)] = 1.0
        row[t_lo_index(h)] = -(p_full_1h - p_d_1h)
        ineq_rows.append(row)
        ineq_rhs.append(p_d_1h * frac)

        # peak >= ch[h]
        row = np.zeros(n_vars)
        row[ch_index(h)] = 1.0
        row[peak_idx] = -1.0
        ineq_rows.append(row)
        ineq_rhs.append(0.0)

    lb[peak_idx] = 0.0
    ub[peak_idx] = p_full_1h


def mark_preference_integrality(
    integrality: np.ndarray,
    *,
    b_hi_index: Callable[[int], int],
    n_hours: int,
) -> None:
    for h in range(n_hours):
        integrality[b_hi_index(h)] = 1
