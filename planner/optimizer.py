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
    effective_soc_floor_kwh,
    max_power_for_hour,
    soc_kwh,
)
from planner.config import PLANNER_BATTERY_CYCLE_COST_PLN, planner_scenario_optimizer_enabled
from planner.hour_remainder import balance_rhs_kwh, remaining_battery_delta_kwh
from planner.models import HourInputs, HourPlan, ScenariosDetail
from planner.night_grid_policy import (
    add_green_stock_constraints,
    add_night_latch_constraints,
    green_stock_n_vars,
    green_var_indexers,
    night_latch_layout,
    resolve_green0_kwh,
)

log = logging.getLogger("planner")


@dataclass
class OptimizeResult:
    hours: list[HourPlan]
    total_cashflow_pln: float
    soc_trajectory_pct: list[float]
    scenario_meta: dict | None = None
    scenarios_detail: ScenariosDetail | None = None


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
    night_charge_carry_in: bool = False,
    green_stock_kwh: float | None = None,
) -> tuple[np.ndarray, float] | None:
    """
    MILP: max Σ (RCE·export − import·import − wear).

    Wear: ``PLANNER_BATTERY_CYCLE_COST_PLN`` × kWh rozładowania (ład bez kary).
    Binarne z_h wymuszają wyłączność import/eksport (brak „mielenia” licznika).
    """
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    wear_per_dis_kwh = cycle_cost if cycle_cost > 0.0 else 0.0
    n_h = len(hours_in)
    n_vars_core, layout = _var_layout(n_h)
    hour_idx = layout["hour_idx"]
    z_idx = layout["z_idx"]
    _windows, night_pos = night_latch_layout(hours_in)
    n_night = len(night_pos)
    n_green = green_stock_n_vars(n_h)
    n_vars = n_vars_core + 2 * n_night + n_green
    big_m = _big_m(hours_in, params)

    def y_ch_idx(h: int) -> int:
        return n_vars_core + night_pos[h]

    def latch_idx(h: int) -> int:
        return n_vars_core + n_night + night_pos[h]

    green_index, ch_pv_index, dis_g_index = green_var_indexers(
        n_vars_core + 2 * n_night, n_h
    )

    c = np.zeros(n_vars)
    for h, hin in enumerate(hours_in):
        i = hour_idx(h, layout["imp"])
        e = hour_idx(h, layout["exp"])
        dis = hour_idx(h, layout["dis"])
        c[i] = hin.import_pln_per_kwh
        c[e] = -hin.export_pln_per_kwh
        c[dis] += wear_per_dis_kwh

    soc0_kwh = soc_kwh(soc_start_pct, params)
    soc_floor = effective_soc_floor_kwh(soc_start_pct, params)
    soc_max = soc_kwh(params.soc_max_pct, params)
    green0 = resolve_green0_kwh(green_stock_kwh, soc0_kwh)
    eta1 = params.eta_one_way

    lb = np.zeros(n_vars)
    ub = np.full(n_vars, np.inf)

    eq_rows: list[np.ndarray] = []
    eq_rhs: list[float] = []

    row = np.zeros(n_vars)
    row[0] = 1.0
    eq_rows.append(row)
    eq_rhs.append(soc0_kwh)

    # params.eta = η_rt; w SOC: +√η·ch − dis/√η (cykl AC→AC = η_rt).
    for h in range(n_h):
        hin = hours_in[h]

        row = np.zeros(n_vars)
        row[h] = -1.0
        row[h + 1] = 1.0
        row[hour_idx(h, layout["ch"])] = -eta1
        row[hour_idx(h, layout["dis"])] = 1.0 / eta1
        eq_rows.append(row)
        eq_rhs.append(0.0)

        row = np.zeros(n_vars)
        row[hour_idx(h, layout["dis"])] = 1.0
        row[hour_idx(h, layout["imp"])] = 1.0
        row[hour_idx(h, layout["ch"])] = -1.0
        row[hour_idx(h, layout["exp"])] = -1.0
        eq_rows.append(row)
        eq_rhs.append(balance_rhs_kwh(hin))

    ineq_rows: list[np.ndarray] = []
    ineq_rhs: list[float] = []
    add_green_stock_constraints(
        eq_rows,
        eq_rhs,
        ineq_rows,
        ineq_rhs,
        n_vars=n_vars,
        n_hours=n_h,
        eta1=eta1,
        green0_kwh=green0,
        soc_max_kwh=soc_max,
        soc_index=lambda h: h,
        ch_index=lambda h: hour_idx(h, layout["ch"]),
        dis_index=lambda h: hour_idx(h, layout["dis"]),
        exp_index=lambda h: hour_idx(h, layout["exp"]),
        green_index=green_index,
        ch_pv_index=ch_pv_index,
        dis_g_index=dis_g_index,
        pv_kwh_of=lambda h: max(0.0, float(hours_in[h].pv_kwh)),
        pmax_of=lambda h: max_power_for_hour(hours_in[h], params),
        lb=lb,
        ub=ub,
    )

    a_eq = np.vstack(eq_rows)
    eq_constraint = LinearConstraint(a_eq, eq_rhs, eq_rhs)

    # imp_h <= M·(1 − z_h),  exp_h <= M·z_h
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

    if n_night > 0:
        add_night_latch_constraints(
            ineq_rows,
            ineq_rhs,
            n_vars=n_vars,
            hours_in=hours_in,
            ch_index=lambda h: hour_idx(h, layout["ch"]),
            exp_index=lambda h: hour_idx(h, layout["exp"]),
            y_ch_index=y_ch_idx,
            latch_index=latch_idx,
            pmax_of=lambda h: max_power_for_hour(hours_in[h], params),
            big_m=big_m,
            carry_in=night_charge_carry_in,
        )

    a_ub = np.vstack(ineq_rows)
    ub_constraint = LinearConstraint(a_ub, -np.inf * np.ones(len(ineq_rhs)), np.array(ineq_rhs))

    for h in range(n_h + 1):
        lb[h] = soc_floor
        ub[h] = soc_max
    for h in range(n_h):
        p_h = max_power_for_hour(hours_in[h], params)
        ub[hour_idx(h, layout["ch"])] = p_h
        ub[hour_idx(h, layout["dis"])] = p_h
        lb[z_idx(h)] = 0.0
        ub[z_idx(h)] = 1.0
    for h in night_pos:
        lb[y_ch_idx(h)] = 0.0
        ub[y_ch_idx(h)] = 1.0
        lb[latch_idx(h)] = 0.0
        ub[latch_idx(h)] = 1.0

    integrality = np.zeros(n_vars, dtype=np.int8)
    for h in range(n_h):
        integrality[z_idx(h)] = 1
    for h in night_pos:
        integrality[y_ch_idx(h)] = 1
        integrality[latch_idx(h)] = 1

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
    night_charge_carry_in: bool = False,
    green_stock_kwh: float | None = None,
) -> OptimizeResult:
    """
    MILP z ciągłym SOC i net_kwh — bez siatki 0,25 kWh.

    Maksymalizuje sumę cashflow (sieć − amortyzacja baterii) przy modelu z η.
    """
    bp = params or BatteryParams()
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    if not hours_in:
        return OptimizeResult(hours=[], total_cashflow_pln=0.0, soc_trajectory_pct=[soc_start_pct])

    if planner_scenario_optimizer_enabled():
        from planner.scenario_optimizer import optimize_horizon_scenarios

        return optimize_horizon_scenarios(
            hours_in,
            soc_start_pct=soc_start_pct,
            params=bp,
            night_charge_carry_in=night_charge_carry_in,
            green_stock_kwh=green_stock_kwh,
        )

    solved = _solve_milp(
        hours_in,
        soc_start_pct=soc_start_pct,
        params=bp,
        night_charge_carry_in=night_charge_carry_in,
        green_stock_kwh=green_stock_kwh,
    )
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
        bd = remaining_battery_delta_kwh(hin, net)
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
        bd = remaining_battery_delta_kwh(hin, net)
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
