"""MILP wieloscenariuszowy: osobne ch/dis/soc per scenariusz, max ważonego E[cashflow]."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from economics import battery_wear_pln_for_hour, cashflow_pln_for_hour
from planner.battery import BatteryParams, battery_delta_from_net, max_power_for_hour, soc_kwh
from planner.config import PLANNER_BATTERY_CYCLE_COST_PLN
from planner.models import HourInputs, HourPlan
from planner.optimizer import OptimizeResult, _big_m, _soc_pct, _solve_milp
from planner.scenarios import PlanningScenario, base_scenario_index, build_planning_scenarios

log = logging.getLogger("planner")

_SIMULTANEOUS_PENALTY = 1e-4


@dataclass
class ScenarioOptimizeMeta:
    """Metadane solve — do audytu / debug."""

    scenarios: list[PlanningScenario]
    expected_cashflow_pln: float
    scenario_cashflow_pln: list[float]


def _scenario_block_size(n_hours: int) -> int:
    """soc[H+1] + ch[H] + dis[H] + imp[H] + exp[H] + z[H]."""
    return 6 * n_hours + 1


def _scenario_var_layout(n_scenarios: int, n_hours: int) -> tuple[int, dict]:
    """
    Zmienne per scenariusz ``s``:
    soc[s,0..H], ch[s,0..H-1], dis[s,0..H-1], imp[s,h], exp[s,h], z[s,h].
    """
    block = _scenario_block_size(n_hours)

    def scenario_base(s: int) -> int:
        return s * block

    def soc_idx(s: int, h: int) -> int:
        return scenario_base(s) + h

    def ch_idx(s: int, h: int) -> int:
        return scenario_base(s) + (n_hours + 1) + h

    def dis_idx(s: int, h: int) -> int:
        return scenario_base(s) + (n_hours + 1) + n_hours + h

    def imp_idx(s: int, h: int) -> int:
        return scenario_base(s) + (n_hours + 1) + 2 * n_hours + 2 * h

    def exp_idx(s: int, h: int) -> int:
        return imp_idx(s, h) + 1

    def z_idx(s: int, h: int) -> int:
        return scenario_base(s) + (n_hours + 1) + 4 * n_hours + h

    n_vars = n_scenarios * block
    return n_vars, {
        "n_hours": n_hours,
        "n_scenarios": n_scenarios,
        "soc_idx": soc_idx,
        "ch_idx": ch_idx,
        "dis_idx": dis_idx,
        "z_idx": z_idx,
        "imp_idx": imp_idx,
        "exp_idx": exp_idx,
    }


def _solve_scenario_milp(
    hours_in: list[HourInputs],
    scenarios: list[PlanningScenario],
    *,
    soc_start_pct: float,
    params: BatteryParams,
) -> tuple[np.ndarray, ScenarioOptimizeMeta] | None:
    """max Σ_s π_s·cashflow_s (sieć − wear per scenariusz)."""
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    wear_per_dis = cycle_cost if cycle_cost > 0.0 else 0.0
    n_h = len(hours_in)
    n_s = len(scenarios)
    if n_h == 0 or n_s == 0:
        return None

    n_vars, layout = _scenario_var_layout(n_s, n_h)
    soc_idx = layout["soc_idx"]
    ch_idx = layout["ch_idx"]
    dis_idx = layout["dis_idx"]
    z_idx = layout["z_idx"]
    imp_idx = layout["imp_idx"]
    exp_idx = layout["exp_idx"]

    big_m = _big_m(hours_in, params)
    c = np.zeros(n_vars)

    for s, sc in enumerate(scenarios):
        pi = float(sc.weight)
        for h, hin in enumerate(hours_in):
            c[imp_idx(s, h)] += pi * hin.import_pln_per_kwh
            c[exp_idx(s, h)] -= pi * hin.export_pln_per_kwh
            c[ch_idx(s, h)] += pi * _SIMULTANEOUS_PENALTY
            c[dis_idx(s, h)] += pi * (_SIMULTANEOUS_PENALTY + wear_per_dis)

    eq_rows: list[np.ndarray] = []
    eq_rhs: list[float] = []
    soc0 = soc_kwh(soc_start_pct, params)
    eta = params.eta

    for s in range(n_s):
        row = np.zeros(n_vars)
        row[soc_idx(s, 0)] = 1.0
        eq_rows.append(row)
        eq_rhs.append(soc0)

        for h in range(n_h):
            row = np.zeros(n_vars)
            row[soc_idx(s, h)] = -1.0
            row[soc_idx(s, h + 1)] = 1.0
            row[ch_idx(s, h)] = -eta
            row[dis_idx(s, h)] = 1.0 / eta
            eq_rows.append(row)
            eq_rhs.append(0.0)

        sc = scenarios[s]
        for h in range(n_h):
            row = np.zeros(n_vars)
            row[dis_idx(s, h)] = 1.0
            row[imp_idx(s, h)] = 1.0
            row[ch_idx(s, h)] = -1.0
            row[exp_idx(s, h)] = -1.0
            eq_rows.append(row)
            eq_rhs.append(float(sc.load_kwh[h]) - float(sc.pv_kwh[h]))

    eq_constraint = LinearConstraint(np.vstack(eq_rows), eq_rhs, eq_rhs)

    exclusivity_rows: list[np.ndarray] = []
    exclusivity_ub: list[float] = []
    for s in range(n_s):
        for h in range(n_h):
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

    soc_min = soc_kwh(params.soc_min_pct, params)
    soc_max = soc_kwh(params.soc_max_pct, params)

    lb = np.zeros(n_vars)
    ub = np.full(n_vars, np.inf)
    for s in range(n_s):
        for h in range(n_h + 1):
            lb[soc_idx(s, h)] = soc_min
            ub[soc_idx(s, h)] = soc_max
        for h in range(n_h):
            p_h = max_power_for_hour(hours_in[h], params)
            ub[ch_idx(s, h)] = p_h
            ub[dis_idx(s, h)] = p_h
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
        constraints=[eq_constraint, exclusivity_constraint],
    )
    if not res.success:
        log.warning("scenario MILP failed: %s", res.message)
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
                float(x[ch_idx(s, h)]),
                float(x[dis_idx(s, h)]),
                cycle_cost_pln=cycle_cost,
            )
            for h in range(n_h)
        )
        scenario_cf.append(grid - wear)

    expected = sum(sc.weight * cf for sc, cf in zip(scenarios, scenario_cf, strict=True))
    meta = ScenarioOptimizeMeta(
        scenarios=scenarios,
        expected_cashflow_pln=expected,
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
    """Fallback: deterministyczny MILP (p50) gdy scenario MILP nie ma rozwiązania."""
    from economics import battery_wear_pln_for_hour, cashflow_pln_for_hour
    from planner.optimizer import _var_layout

    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    solved = _solve_milp(hours_in, soc_start_pct=soc_start_pct, params=params)
    if solved is None:
        log.error("scenario optimizer: deterministic MILP też failed po %s — brak planu", reason)
        from planner.optimizer import _fallback_neutral

        return _fallback_neutral(hours_in, soc_start_pct, params)

    log.warning("scenario optimizer: %s — fallback deterministyczny MILP (p50)", reason)
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
        scenario_meta={"scenario_milp_failed": True, "fallback": "deterministic_p50"},
    )


def _scenario_meta_dict(meta: ScenarioOptimizeMeta) -> dict:
    return {
        "model": "per_scenario_ch_dis_soc",
        "expected_cashflow_pln": meta.expected_cashflow_pln,
        "scenario_cashflow_pln": {
            sc.name: cf
            for sc, cf in zip(meta.scenarios, meta.scenario_cashflow_pln, strict=True)
        },
        "scenario_weights": {sc.name: sc.weight for sc in meta.scenarios},
    }


def optimize_horizon_scenarios(
    hours_in: list[HourInputs],
    *,
    soc_start_pct: float,
    params: BatteryParams | None = None,
) -> OptimizeResult:
    """
    Wieloscenariuszowy MILP: max ważonego E[cashflow].

    Per scenariusz: osobne ch/dis/soc/imp/exp. Plan wykonawczy ze scenariusza bazowego (p50).
    """
    bp = params or BatteryParams()
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    if not hours_in:
        return OptimizeResult(hours=[], total_cashflow_pln=0.0, soc_trajectory_pct=[soc_start_pct])

    scenarios = build_planning_scenarios(hours_in)
    solved = _solve_scenario_milp(
        hours_in,
        scenarios,
        soc_start_pct=soc_start_pct,
        params=bp,
    )
    if solved is None:
        return _optimize_from_deterministic_milp(
            hours_in,
            soc_start_pct=soc_start_pct,
            params=bp,
            reason="scenario MILP infeasible/unbounded",
        )

    x, meta = solved
    n_h = len(hours_in)
    n_s = len(scenarios)
    _, layout = _scenario_var_layout(n_s, n_h)
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
        ch = float(x[ch_idx(s_base, h)])
        dis = float(x[dis_idx(s_base, h)])
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

    meta_dict = _scenario_meta_dict(meta)
    log.info(
        "scenario MILP solved: E[cashflow]=%.2f scenarios=%s",
        meta.expected_cashflow_pln,
        meta_dict["scenario_cashflow_pln"],
    )

    return OptimizeResult(
        hours=plans,
        total_cashflow_pln=meta.expected_cashflow_pln,
        soc_trajectory_pct=traj,
        scenario_meta=meta_dict,
    )
