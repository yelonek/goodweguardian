"""MILP wieloscenariuszowy z CVaR (Rockafellar–Uryasev)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from economics import battery_wear_pln_for_hour, cashflow_pln_for_hour
from planner.battery import BatteryParams, battery_delta_from_net, soc_kwh
from planner.config import (
    PLANNER_BATTERY_CYCLE_COST_PLN,
    PLANNER_CVAR_ALPHA,
)
from planner.models import HourInputs, HourPlan
from planner.optimizer import OptimizeResult, _big_m, _fallback_neutral, _soc_pct
from planner.scenarios import PlanningScenario, base_scenario_index, build_planning_scenarios

log = logging.getLogger("planner")

_SIMULTANEOUS_PENALTY = 1e-4


@dataclass
class RiskOptimizeMeta:
    """Metadane solve — do audytu / debug."""

    scenarios: list[PlanningScenario]
    cvar_alpha: float
    cvar_lambda: float
    expected_cashflow_pln: float
    cvar_penalty_pln: float
    scenario_cashflow_pln: list[float]


def _risk_var_layout(
    n_scenarios: int,
    n_hours: int,
) -> tuple[int, dict]:
    """
    Zmienne:
    - soc[s,0..H]
    - ch[0..H-1], dis[0..H-1]  (wspólne)
    - imp[s,h], exp[s,h], z[s,h]  (per scenariusz)
    - zeta, u[s]  (CVaR)
    """
    n_soc = n_scenarios * (n_hours + 1)
    n_shared = 2 * n_hours  # ch, dis — wspólne
    n_grid = n_scenarios * 2 * n_hours
    n_z = n_scenarios * n_hours
    n_cvar = 1 + n_scenarios

    def soc_idx(s: int, h: int) -> int:
        return s * (n_hours + 1) + h

    shared_base = n_soc

    def ch_idx(h: int) -> int:
        return shared_base + h

    def dis_idx(h: int) -> int:
        return shared_base + n_hours + h

    grid_base = shared_base + n_shared

    def imp_idx(s: int, h: int) -> int:
        return grid_base + s * 2 * n_hours + 2 * h

    def exp_idx(s: int, h: int) -> int:
        return grid_base + s * 2 * n_hours + 2 * h + 1

    z_base = grid_base + n_grid

    def z_idx(s: int, h: int) -> int:
        return z_base + s * n_hours + h

    cvar_base = z_base + n_z

    def zeta_idx() -> int:
        return cvar_base

    def u_idx(s: int) -> int:
        return cvar_base + 1 + s

    n_vars = cvar_base + n_cvar
    return n_vars, {
        "n_hours": n_hours,
        "n_scenarios": n_scenarios,
        "soc_idx": soc_idx,
        "ch_idx": ch_idx,
        "dis_idx": dis_idx,
        "z_idx": z_idx,
        "imp_idx": imp_idx,
        "exp_idx": exp_idx,
        "zeta_idx": zeta_idx,
        "u_idx": u_idx,
    }


def _solve_risk_milp(
    hours_in: list[HourInputs],
    scenarios: list[PlanningScenario],
    *,
    soc_start_pct: float,
    params: BatteryParams,
    cvar_lambda: float,
    cvar_alpha: float,
    soc_bound_scenario: int | None = None,
) -> tuple[np.ndarray, RiskOptimizeMeta] | None:
    """
    max  E[cashflow] − λ·CVaR_α(−cashflow_dzienny) − wear

    Rockafellar–Uryasev; scipy ``milp`` minimalizuje.
    """
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    wear_per_dis = cycle_cost if cycle_cost > 0.0 else 0.0
    n_h = len(hours_in)
    n_s = len(scenarios)
    if n_h == 0 or n_s == 0:
        return None

    n_vars, layout = _risk_var_layout(n_s, n_h)
    soc_idx = layout["soc_idx"]
    ch_idx = layout["ch_idx"]
    dis_idx = layout["dis_idx"]
    z_idx = layout["z_idx"]
    imp_idx = layout["imp_idx"]
    exp_idx = layout["exp_idx"]
    zeta_i = layout["zeta_idx"]()
    u_idx = layout["u_idx"]

    big_m = _big_m(hours_in, params)
    alpha = max(0.01, min(0.99, float(cvar_alpha)))
    lam = max(0.0, float(cvar_lambda))
    inv_tail = 1.0 / (1.0 - alpha)

    c = np.zeros(n_vars)

    for s, sc in enumerate(scenarios):
        pi = float(sc.weight)
        for h, hin in enumerate(hours_in):
            c[imp_idx(s, h)] += pi * hin.import_pln_per_kwh
            c[exp_idx(s, h)] -= pi * hin.export_pln_per_kwh
        c[u_idx(s)] += lam * pi * inv_tail

    c[zeta_i] += lam

    for h in range(n_h):
        c[ch_idx(h)] += _SIMULTANEOUS_PENALTY
        c[dis_idx(h)] += _SIMULTANEOUS_PENALTY + wear_per_dis

    eq_rows: list[np.ndarray] = []
    eq_rhs: list[float] = []

    soc0 = soc_kwh(soc_start_pct, params)
    for s in range(n_s):
        row = np.zeros(n_vars)
        row[soc_idx(s, 0)] = 1.0
        eq_rows.append(row)
        eq_rhs.append(soc0)

    eta = params.eta
    for s in range(n_s):
        sc = scenarios[s]
        for h in range(n_h):
            row = np.zeros(n_vars)
            row[soc_idx(s, h)] = -1.0
            row[soc_idx(s, h + 1)] = 1.0
            row[ch_idx(h)] = -eta
            row[dis_idx(h)] = 1.0 / eta
            eq_rows.append(row)
            eq_rhs.append(0.0)

            row = np.zeros(n_vars)
            row[dis_idx(h)] = 1.0
            row[imp_idx(s, h)] = 1.0
            row[ch_idx(h)] = -1.0
            row[exp_idx(s, h)] = -1.0
            eq_rows.append(row)
            eq_rhs.append(float(sc.load_kwh[h]) - float(sc.pv_kwh[h]))

    eq_constraint = LinearConstraint(np.vstack(eq_rows), eq_rhs, eq_rhs)

    ineq_rows: list[np.ndarray] = []
    ineq_lb: list[float] = []
    for h in range(n_h):
        for s in range(n_s):
            row = np.zeros(n_vars)
            row[imp_idx(s, h)] = 1.0
            row[z_idx(s, h)] = big_m
            ineq_rows.append(row)
            ineq_lb.append(big_m)

            row = np.zeros(n_vars)
            row[exp_idx(s, h)] = 1.0
            row[z_idx(s, h)] = -big_m
            ineq_rows.append(row)
            ineq_lb.append(0.0)

    for s in range(n_s):
        row = np.zeros(n_vars)
        row[u_idx(s)] = 1.0
        row[zeta_i] = 1.0
        for h, hin in enumerate(hours_in):
            row[exp_idx(s, h)] += hin.export_pln_per_kwh
            row[imp_idx(s, h)] -= hin.import_pln_per_kwh
        for h in range(n_h):
            row[dis_idx(h)] -= wear_per_dis
        ineq_rows.append(row)
        ineq_lb.append(0.0)

    ineq_constraint = LinearConstraint(
        np.vstack(ineq_rows),
        np.array(ineq_lb),
        np.full(len(ineq_lb), np.inf),
    )

    soc_min = soc_kwh(params.soc_min_pct, params)
    soc_max = soc_kwh(params.soc_max_pct, params)
    p_max = params.max_power_kwh_per_h
    s_soc = soc_bound_scenario if soc_bound_scenario is not None else base_scenario_index(scenarios)

    lb = np.zeros(n_vars)
    ub = np.full(n_vars, np.inf)
    for s in range(n_s):
        for h in range(n_h + 1):
            if s == s_soc:
                lb[soc_idx(s, h)] = soc_min
                ub[soc_idx(s, h)] = soc_max
            else:
                lb[soc_idx(s, h)] = 0.0
                ub[soc_idx(s, h)] = soc_max
    for h in range(n_h):
        ub[ch_idx(h)] = p_max
        ub[dis_idx(h)] = p_max
    for s in range(n_s):
        for h in range(n_h):
            lb[z_idx(s, h)] = 0.0
            ub[z_idx(s, h)] = 1.0

    integrality = np.zeros(n_vars, dtype=np.int8)
    for s in range(n_s):
        for h in range(n_h):
            integrality[z_idx(s, h)] = 1

    res = milp(
        c=c,
        integrality=integrality,
        bounds=Bounds(lb, ub),
        constraints=[eq_constraint, ineq_constraint],
    )
    if not res.success:
        log.warning("risk MILP failed: %s", res.message)
        return None

    x = res.x
    scenario_cf: list[float] = []
    for s in range(n_s):
        grid = 0.0
        for h, hin in enumerate(hours_in):
            imp = float(x[imp_idx(s, h)])
            exp = float(x[exp_idx(s, h)])
            grid += cashflow_pln_for_hour(
                exp - imp,
                rce_pln_per_kwh=hin.export_pln_per_kwh,
                import_pln_per_kwh=hin.import_pln_per_kwh,
            )
        wear = sum(
            battery_wear_pln_for_hour(
                0.0,
                float(x[dis_idx(h)]),
                cycle_cost_pln=cycle_cost,
            )
            for h in range(n_h)
        )
        scenario_cf.append(grid - wear)

    expected = sum(sc.weight * cf for sc, cf in zip(scenarios, scenario_cf, strict=True))
    zeta = float(x[zeta_i])
    cvar = zeta + inv_tail * sum(
        sc.weight * float(x[u_idx(s)]) for s, sc in enumerate(scenarios)
    )
    meta = RiskOptimizeMeta(
        scenarios=scenarios,
        cvar_alpha=alpha,
        cvar_lambda=lam,
        expected_cashflow_pln=expected,
        cvar_penalty_pln=lam * cvar,
        scenario_cashflow_pln=scenario_cf,
    )
    return x, meta


def _risk_meta_dict(meta: RiskOptimizeMeta) -> dict:
    return {
        "cvar_lambda": meta.cvar_lambda,
        "cvar_alpha": meta.cvar_alpha,
        "expected_cashflow_pln": meta.expected_cashflow_pln,
        "cvar_penalty_pln": meta.cvar_penalty_pln,
        "objective_pln": meta.expected_cashflow_pln - meta.cvar_penalty_pln,
        "scenario_cashflow_pln": {
            sc.name: cf
            for sc, cf in zip(meta.scenarios, meta.scenario_cashflow_pln, strict=True)
        },
        "scenario_weights": {sc.name: sc.weight for sc in meta.scenarios},
    }


def optimize_horizon_cvar(
    hours_in: list[HourInputs],
    *,
    soc_start_pct: float,
    params: BatteryParams | None = None,
    cvar_lambda: float | None = None,
    cvar_alpha: float | None = None,
) -> OptimizeResult:
    """
    Optymalizacja z niepewnością prognoz i karą CVaR za złe scenariusze.

    Plan raportowany (net, SOC) po scenariuszu **bazowym** (p50); decyzja ch/dis wspólna.
    """
    bp = params or BatteryParams()
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    if not hours_in:
        return OptimizeResult(hours=[], total_cashflow_pln=0.0, soc_trajectory_pct=[soc_start_pct])

    scenarios = build_planning_scenarios(hours_in)
    if cvar_lambda is not None:
        lam = float(cvar_lambda)
    else:
        from planner.cvar_calibrate import get_effective_cvar_lambda

        lam = get_effective_cvar_lambda()
    alpha = float(PLANNER_CVAR_ALPHA if cvar_alpha is None else cvar_alpha)

    solved = _solve_risk_milp(
        hours_in,
        scenarios,
        soc_start_pct=soc_start_pct,
        params=bp,
        cvar_lambda=lam,
        cvar_alpha=alpha,
    )
    if solved is None:
        log.warning("risk optimizer: fallback neutralny")
        return _fallback_neutral(hours_in, soc_start_pct, bp)

    x, meta = solved
    n_h = len(hours_in)
    n_s = len(scenarios)
    _, layout = _risk_var_layout(n_s, n_h)
    soc_idx = layout["soc_idx"]
    ch_idx = layout["ch_idx"]
    dis_idx = layout["dis_idx"]
    imp_idx = layout["imp_idx"]
    exp_idx = layout["exp_idx"]
    s_base = base_scenario_index(scenarios)

    plans: list[HourPlan] = []
    traj: list[float] = [_soc_pct(float(x[soc_idx(s_base, 0)]), bp)]

    for h, hin in enumerate(hours_in):
        soc_start = _soc_pct(float(x[soc_idx(s_base, h)]), bp)
        imp = float(x[imp_idx(s_base, h)])
        exp = float(x[exp_idx(s_base, h)])
        ch = float(x[ch_idx(h)])
        dis = float(x[dis_idx(h)])
        net = exp - imp
        bd = battery_delta_from_net(pv_kwh=hin.pv_kwh, load_kwh=hin.load_kwh, net_kwh=net)
        soc_end = _soc_pct(float(x[soc_idx(s_base, h + 1)]), bp)
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

    meta_dict = _risk_meta_dict(meta)
    log.info(
        "risk MILP solved: λ=%.3g α=%.2f E[cashflow]=%.2f cvar_penalty=%.2f objective=%.2f scenarios=%s",
        meta.cvar_lambda,
        meta.cvar_alpha,
        meta.expected_cashflow_pln,
        meta.cvar_penalty_pln,
        meta.expected_cashflow_pln - meta.cvar_penalty_pln,
        meta_dict["scenario_cashflow_pln"],
    )

    return OptimizeResult(
        hours=plans,
        total_cashflow_pln=meta.expected_cashflow_pln - meta.cvar_penalty_pln,
        soc_trajectory_pct=traj,
        risk_meta=meta_dict,
    )
