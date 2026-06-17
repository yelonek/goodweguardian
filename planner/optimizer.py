"""Optymalizator MILP/LP: maksymalizacja cashflow PLN na horyzoncie godzin."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from economics import battery_wear_pln_for_hour, cashflow_pln_for_hour
from planner.battery import (
    BatteryParams,
    apply_battery_step,
    battery_delta_from_net,
    soc_kwh,
)
from planner.config import PLANNER_BATTERY_CYCLE_COST_PLN, planner_risk_optimizer_enabled
from planner.models import HourInputs, HourPlan

log = logging.getLogger("planner")

# Kara za jednoczesne ładowanie i rozładowanie (degeneracja numeryczna).
_SIMULTANEOUS_PENALTY = 1e-4


@dataclass
class OptimizeResult:
    hours: list[HourPlan]
    total_cashflow_pln: float
    soc_trajectory_pct: list[float]
    risk_meta: dict | None = None


def _big_m(hours_in: list[HourInputs], params: BatteryParams) -> float:
    peak = max(
        (max(h.pv_kwh, h.load_kwh) for h in hours_in),
        default=0.0,
    )
    return max(peak + params.max_power_kwh_per_h, params.max_power_kwh_per_h * 2.0, 1.0)


def _var_layout(n_hours: int) -> tuple[int, dict[str, int]]:
    """
    Kolejność: soc[0..H], (imp, exp, ch, dis)×H, z[0..H-1] (binarne: 1=eksport).
    """
    n_soc = n_hours + 1
    n_flow = 4 * n_hours
    base_flow = n_soc
    base_z = n_soc + n_flow

    def hour_idx(h: int, field: int) -> int:
        return base_flow + 4 * h + field

    def z_idx(h: int) -> int:
        return base_z + h

    return base_z + n_hours, {
        "n_soc": n_soc,
        "imp": 0,
        "exp": 1,
        "ch": 2,
        "dis": 3,
        "hour_idx": hour_idx,
        "z_idx": z_idx,
    }


def _solve_milp(
    hours_in: list[HourInputs],
    *,
    soc_start_pct: float,
    params: BatteryParams,
) -> tuple[np.ndarray, float] | None:
    """
    MILP: max Σ (RCE·export − import·import − wear).

    Wear: ``PLANNER_BATTERY_CYCLE_COST_PLN`` × kWh rozładowania (ład bez kary).
    Binarne z_h wymuszają wyłączność import/eksport (brak „mielenia” licznika).
    """
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    wear_per_dis_kwh = cycle_cost if cycle_cost > 0.0 else 0.0
    n_h = len(hours_in)
    n_vars, layout = _var_layout(n_h)
    hour_idx = layout["hour_idx"]
    z_idx = layout["z_idx"]
    big_m = _big_m(hours_in, params)

    c = np.zeros(n_vars)
    for h, hin in enumerate(hours_in):
        i = hour_idx(h, layout["imp"])
        e = hour_idx(h, layout["exp"])
        ch = hour_idx(h, layout["ch"])
        dis = hour_idx(h, layout["dis"])
        c[i] = hin.import_pln_per_kwh
        c[e] = -hin.export_pln_per_kwh
        c[ch] += _SIMULTANEOUS_PENALTY
        c[dis] += _SIMULTANEOUS_PENALTY + wear_per_dis_kwh

    eq_rows: list[np.ndarray] = []
    eq_rhs: list[float] = []

    soc0_kwh = soc_kwh(soc_start_pct, params)
    row = np.zeros(n_vars)
    row[0] = 1.0
    eq_rows.append(row)
    eq_rhs.append(soc0_kwh)

    eta = params.eta
    for h in range(n_h):
        hin = hours_in[h]

        row = np.zeros(n_vars)
        row[h] = -1.0
        row[h + 1] = 1.0
        row[hour_idx(h, layout["ch"])] = -eta
        row[hour_idx(h, layout["dis"])] = 1.0 / eta
        eq_rows.append(row)
        eq_rhs.append(0.0)

        row = np.zeros(n_vars)
        row[hour_idx(h, layout["dis"])] = 1.0
        row[hour_idx(h, layout["imp"])] = 1.0
        row[hour_idx(h, layout["ch"])] = -1.0
        row[hour_idx(h, layout["exp"])] = -1.0
        eq_rows.append(row)
        eq_rhs.append(hin.load_kwh - hin.pv_kwh)

    a_eq = np.vstack(eq_rows)
    eq_constraint = LinearConstraint(a_eq, eq_rhs, eq_rhs)

    # imp_h <= M·(1 − z_h),  exp_h <= M·z_h
    ineq_rows: list[np.ndarray] = []
    ineq_rhs: list[float] = []
    for h in range(n_h):
        row = np.zeros(n_vars)
        row[hour_idx(h, layout["imp"])] = 1.0
        row[z_idx(h)] = big_m
        ineq_rows.append(row)
        ineq_rhs.append(big_m)

        row = np.zeros(n_vars)
        row[hour_idx(h, layout["exp"])] = 1.0
        row[z_idx(h)] = -big_m
        ineq_rows.append(row)
        ineq_rhs.append(0.0)

    a_ub = np.vstack(ineq_rows)
    ub_constraint = LinearConstraint(a_ub, -np.inf * np.ones(len(ineq_rhs)), np.array(ineq_rhs))

    soc_min = soc_kwh(params.soc_min_pct, params)
    soc_max = soc_kwh(params.soc_max_pct, params)
    p_max = params.max_power_kwh_per_h

    lb = np.zeros(n_vars)
    ub = np.full(n_vars, np.inf)
    for h in range(n_h + 1):
        lb[h] = soc_min
        ub[h] = soc_max
    for h in range(n_h):
        ub[hour_idx(h, layout["ch"])] = p_max
        ub[hour_idx(h, layout["dis"])] = p_max
        lb[z_idx(h)] = 0.0
        ub[z_idx(h)] = 1.0

    integrality = np.zeros(n_vars, dtype=np.int8)
    for h in range(n_h):
        integrality[z_idx(h)] = 1

    res = milp(
        c=c,
        integrality=integrality,
        bounds=Bounds(lb, ub),
        constraints=[eq_constraint, ub_constraint],
    )
    if not res.success:
        log.warning("MILP optimizer failed: %s", res.message)
        return None

    total_cf = -float(res.fun)
    return res.x, total_cf


def _soc_pct(energy_kwh: float, params: BatteryParams) -> float:
    if params.capacity_kwh <= 0:
        return 0.0
    return (energy_kwh / params.capacity_kwh) * 100.0


def optimize_horizon(
    hours_in: list[HourInputs],
    *,
    soc_start_pct: float,
    params: BatteryParams | None = None,
) -> OptimizeResult:
    """
    MILP z ciągłym SOC i net_kwh — bez siatki 0,25 kWh.

    Maksymalizuje sumę cashflow (sieć − amortyzacja baterii) przy modelu z η.
    """
    bp = params or BatteryParams()
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    if not hours_in:
        return OptimizeResult(hours=[], total_cashflow_pln=0.0, soc_trajectory_pct=[soc_start_pct])

    if planner_risk_optimizer_enabled():
        from planner.risk_optimizer import optimize_horizon_cvar

        return optimize_horizon_cvar(hours_in, soc_start_pct=soc_start_pct, params=bp)

    solved = _solve_milp(hours_in, soc_start_pct=soc_start_pct, params=bp)
    if solved is None:
        log.warning("optimizer: brak rozwiązania MILP — fallback neutralny")
        return _fallback_neutral(hours_in, soc_start_pct, bp)

    x, total_cf = solved
    n_h = len(hours_in)
    _, layout = _var_layout(n_h)
    hour_idx = layout["hour_idx"]

    plans: list[HourPlan] = []
    traj: list[float] = [_soc_pct(float(x[0]), bp)]

    for h, hin in enumerate(hours_in):
        soc_start = _soc_pct(float(x[h]), bp)
        imp = float(x[hour_idx(h, layout["imp"])])
        exp = float(x[hour_idx(h, layout["exp"])])
        ch = float(x[hour_idx(h, layout["ch"])])
        dis = float(x[hour_idx(h, layout["dis"])])
        net = exp - imp
        bd = battery_delta_from_net(pv_kwh=hin.pv_kwh, load_kwh=hin.load_kwh, net_kwh=net)
        soc_end = _soc_pct(float(x[h + 1]), bp)
        grid_cf = cashflow_pln_for_hour(
            net,
            rce_pln_per_kwh=hin.export_pln_per_kwh,
            import_pln_per_kwh=hin.import_pln_per_kwh,
        )
        wear = battery_wear_pln_for_hour(ch, dis, cycle_cost_pln=cycle_cost)
        plans.append(
            HourPlan(
                date=hin.date,
                hour=hin.hour,
                target_net_kwh=net,
                expected_cashflow_pln=grid_cf - wear,
                battery_wear_cost_pln=wear,
                soc_start_pct=soc_start,
                soc_end_pct=soc_end,
                battery_delta_kwh=bd,
            )
        )
        traj.append(soc_end)

    return OptimizeResult(hours=plans, total_cashflow_pln=total_cf, soc_trajectory_pct=traj)


def _fallback_neutral(
    hours_in: list[HourInputs],
    soc_start_pct: float,
    bp: BatteryParams,
) -> OptimizeResult:
    plans: list[HourPlan] = []
    soc = soc_start_pct
    total = 0.0
    traj = [soc]
    for hin in hours_in:
        net = 0.0
        bd = battery_delta_from_net(pv_kwh=hin.pv_kwh, load_kwh=hin.load_kwh, net_kwh=net)
        soc_new = apply_battery_step(soc, bd, bp) or soc
        cf = cashflow_pln_for_hour(
            net,
            rce_pln_per_kwh=hin.export_pln_per_kwh,
            import_pln_per_kwh=hin.import_pln_per_kwh,
        )
        total += cf
        plans.append(
            HourPlan(
                date=hin.date,
                hour=hin.hour,
                target_net_kwh=net,
                expected_cashflow_pln=cf,
                soc_start_pct=soc,
                soc_end_pct=soc_new,
                battery_delta_kwh=bd,
            )
        )
        soc = soc_new
        traj.append(soc)
    return OptimizeResult(hours=plans, total_cashflow_pln=total, soc_trajectory_pct=traj)
