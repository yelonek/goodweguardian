"""MILP wieloscenariuszowy z CVaR (Rockafellar–Uryasev)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from economics import battery_wear_pln_for_hour, cashflow_pln_for_hour
from planner.battery import BatteryParams, battery_delta_from_net, max_power_for_hour, soc_kwh
from planner.config import (
    PLANNER_BATTERY_CYCLE_COST_PLN,
    PLANNER_CVAR_ALPHA,
)
from planner.models import HourInputs, HourPlan
from planner.optimizer import OptimizeResult, _big_m, _soc_pct, _solve_milp
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
    - soc[0..H]  (tylko scenariusz bazowy — wspólne ch/dis nie pozwalają na osobne SOC)
    - ch[0..H-1], dis[0..H-1]  (wspólne)
    - imp[s,h], exp[s,h], z[s,h]  (per scenariusz — bilans sieci)
    - zeta, u[s]  (CVaR)
    """
    n_soc = n_hours + 1
    n_shared = 2 * n_hours  # ch, dis — wspólne
    n_grid = n_scenarios * 2 * n_hours
    n_z = n_scenarios * n_hours
    n_cvar = 1 + n_scenarios

    def soc_idx(h: int) -> int:
        return h

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
    row = np.zeros(n_vars)
    row[soc_idx(0)] = 1.0
    eq_rows.append(row)
    eq_rhs.append(soc0)

    eta = params.eta
    for h in range(n_h):
        row = np.zeros(n_vars)
        row[soc_idx(h)] = -1.0
        row[soc_idx(h + 1)] = 1.0
        row[ch_idx(h)] = -eta
        row[dis_idx(h)] = 1.0 / eta
        eq_rows.append(row)
        eq_rhs.append(0.0)

    for s in range(n_s):
        sc = scenarios[s]
        for h in range(n_h):
            row = np.zeros(n_vars)
            row[dis_idx(h)] = 1.0
            row[imp_idx(s, h)] = 1.0
            row[ch_idx(h)] = -1.0
            row[exp_idx(s, h)] = -1.0
            eq_rows.append(row)
            eq_rhs.append(float(sc.load_kwh[h]) - float(sc.pv_kwh[h]))

    eq_constraint = LinearConstraint(np.vstack(eq_rows), eq_rhs, eq_rhs)

    # imp_h <= M·(1 − z_h),  exp_h <= M·z_h  (jak w deterministycznym MILP)
    exclusivity_rows: list[np.ndarray] = []
    exclusivity_ub: list[float] = []
    for h in range(n_h):
        for s in range(n_s):
            row = np.zeros(n_vars)
            row[imp_idx(s, h)] = 1.0
            row[z_idx(s, h)] = big_m
            exclusivity_rows.append(row)
            exclusivity_ub.append(big_m)

            row = np.zeros(n_vars)
            row[exp_idx(s, h)] = 1.0
            row[z_idx(s, h)] = -big_m
            exclusivity_rows.append(row)
            exclusivity_ub.append(0.0)

    exclusivity_constraint = LinearConstraint(
        np.vstack(exclusivity_rows),
        -np.full(len(exclusivity_ub), np.inf),
        np.array(exclusivity_ub),
    )

    cvar_rows: list[np.ndarray] = []
    cvar_lb: list[float] = []
    for s in range(n_s):
        row = np.zeros(n_vars)
        row[u_idx(s)] = 1.0
        row[zeta_i] = 1.0
        for h, hin in enumerate(hours_in):
            row[exp_idx(s, h)] += hin.export_pln_per_kwh
            row[imp_idx(s, h)] -= hin.import_pln_per_kwh
        for h in range(n_h):
            row[dis_idx(h)] -= wear_per_dis
        cvar_rows.append(row)
        cvar_lb.append(0.0)

    cvar_constraint = LinearConstraint(
        np.vstack(cvar_rows),
        np.array(cvar_lb),
        np.full(len(cvar_lb), np.inf),
    )

    soc_min = soc_kwh(params.soc_min_pct, params)
    soc_max = soc_kwh(params.soc_max_pct, params)

    lb = np.zeros(n_vars)
    ub = np.full(n_vars, np.inf)
    for h in range(n_h + 1):
        lb[soc_idx(h)] = soc_min
        ub[soc_idx(h)] = soc_max
    for h in range(n_h):
        p_h = max_power_for_hour(hours_in[h], params)
        ub[ch_idx(h)] = p_h
        ub[dis_idx(h)] = p_h
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
        constraints=[eq_constraint, exclusivity_constraint, cvar_constraint],
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


def _optimize_from_deterministic_milp(
    hours_in: list[HourInputs],
    *,
    soc_start_pct: float,
    params: BatteryParams,
    reason: str,
) -> OptimizeResult:
    """Fallback: deterministyczny MILP (p50) gdy risk MILP nie ma rozwiązania."""
    from economics import battery_wear_pln_for_hour, cashflow_pln_for_hour
    from planner.optimizer import _var_layout

    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    solved = _solve_milp(hours_in, soc_start_pct=soc_start_pct, params=params)
    if solved is None:
        log.error("risk optimizer: deterministic MILP też failed po %s — brak planu", reason)
        from planner.optimizer import _fallback_neutral

        return _fallback_neutral(hours_in, soc_start_pct, params)

    log.warning("risk optimizer: %s — fallback deterministyczny MILP (p50)", reason)
    x, total_cf = solved
    n_h = len(hours_in)
    _, layout = _var_layout(n_h)
    hour_idx = layout["hour_idx"]

    plans: list[HourPlan] = []
    traj: list[float] = [_soc_pct(float(x[0]), params)]

    for h, hin in enumerate(hours_in):
        soc_start = _soc_pct(float(x[h]), params)
        imp = float(x[hour_idx(h, layout["imp"])])
        exp = float(x[hour_idx(h, layout["exp"])])
        ch = float(x[hour_idx(h, layout["ch"])])
        dis = float(x[hour_idx(h, layout["dis"])])
        net = exp - imp
        bd = battery_delta_from_net(pv_kwh=hin.pv_kwh, load_kwh=hin.load_kwh, net_kwh=net)
        soc_end = _soc_pct(float(x[h + 1]), params)
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

    return OptimizeResult(
        hours=plans,
        total_cashflow_pln=total_cf,
        soc_trajectory_pct=traj,
        risk_meta={"risk_milp_failed": True, "fallback": "deterministic_p50"},
    )


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
        return _optimize_from_deterministic_milp(
            hours_in,
            soc_start_pct=soc_start_pct,
            params=bp,
            reason="risk MILP infeasible/unbounded",
        )

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
    traj: list[float] = [_soc_pct(float(x[soc_idx(0)]), bp)]

    for h, hin in enumerate(hours_in):
        soc_start = _soc_pct(float(x[soc_idx(h)]), bp)
        imp = float(x[imp_idx(s_base, h)])
        exp = float(x[exp_idx(s_base, h)])
        ch = float(x[ch_idx(h)])
        dis = float(x[dis_idx(h)])
        net = exp - imp
        bd = battery_delta_from_net(pv_kwh=hin.pv_kwh, load_kwh=hin.load_kwh, net_kwh=net)
        soc_end = _soc_pct(float(x[soc_idx(h + 1)]), bp)
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
