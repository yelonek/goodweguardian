"""Stochastic eco-slot MILP: wspólne ch/dis/soc, per-scenariusz imp/exp, tryby eco w solve."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from economics import battery_wear_pln_for_hour, cashflow_pln_for_hour
from planner.battery import BatteryParams, battery_delta_from_net, max_power_for_hour, soc_kwh
from planner.config import PLANNER_BATTERY_CYCLE_COST_PLN, PLANNER_EXPORT_PROFIT_MIN_PLN
from planner.ecoslot_scenarios import base_scenario_index, build_ecoslot_scenarios
from planner.models import ExecMode, HourInputs, HourPlan
from planner.optimizer import OptimizeResult, _big_m, _fallback_neutral, _soc_pct, _solve_milp
from planner.scenarios import PlanningScenario

log = logging.getLogger("planner")

_SIMULTANEOUS_PENALTY = 1e-4
_FLOW_EPS = 0.05
_SERVE_DISCHARGE_SLACK_KWH = 0.02
_CHEAP_IMPORT_TOL_PLN = 0.02


def _cheap_import_threshold(hours_in: list[HourInputs]) -> float:
    return min(h.import_pln_per_kwh for h in hours_in) + _CHEAP_IMPORT_TOL_PLN


@dataclass
class EcoslotOptimizeMeta:
    scenarios: list[PlanningScenario]
    expected_cashflow_pln: float
    scenario_cashflow_pln: list[float]


def _var_layout(n_scenarios: int, n_hours: int) -> tuple[int, dict]:
    """
    Wspólne ciągłe: soc[H+1], ch_pv[H], ch_grid[H], dis[H].
    Per scenariusz: imp[S,H], exp[S,H].
    Binarne wspólne: b_charge_grid[H], b_export[H].
    Binarne per scenariusz: z[S,H] (1=eksport).
    """
    n_h = n_hours
    n_s = n_scenarios
    n_soc = n_h + 1
    shared_flow = 3 * n_h
    scen_flow = 2 * n_s * n_h
    shared_bin = 2 * n_h
    scen_bin = n_s * n_h

    def soc_idx(h: int) -> int:
        return h

    def ch_pv_idx(h: int) -> int:
        return n_soc + h

    def ch_grid_idx(h: int) -> int:
        return n_soc + n_h + h

    def dis_idx(h: int) -> int:
        return n_soc + 2 * n_h + h

    scen_base = n_soc + shared_flow

    def imp_idx(s: int, h: int) -> int:
        return scen_base + s * (2 * n_h) + h

    def exp_idx(s: int, h: int) -> int:
        return scen_base + s * (2 * n_h) + n_h + h

    bin_base = scen_base + scen_flow

    def b_cg_idx(h: int) -> int:
        return bin_base + h

    def b_exp_idx(h: int) -> int:
        return bin_base + n_h + h

    def z_idx(s: int, h: int) -> int:
        return bin_base + shared_bin + s * n_h + h

    n_vars = bin_base + shared_bin + scen_bin
    return n_vars, {
        "n_hours": n_h,
        "n_scenarios": n_s,
        "soc_idx": soc_idx,
        "ch_pv_idx": ch_pv_idx,
        "ch_grid_idx": ch_grid_idx,
        "dis_idx": dis_idx,
        "imp_idx": imp_idx,
        "exp_idx": exp_idx,
        "b_cg_idx": b_cg_idx,
        "b_exp_idx": b_exp_idx,
        "z_idx": z_idx,
    }


def _infer_exec_mode(
    *,
    ch_pv: float,
    ch_grid: float,
    dis: float,
    net: float,
    export_pln: float,
    load_kwh: float,
    pv_kwh: float,
    b_cg: float,
    b_exp: float,
) -> ExecMode:
    ch = ch_pv + ch_grid
    if ch_grid > _FLOW_EPS:
        return "charge_grid"
    if (
        dis > _FLOW_EPS
        and net > _FLOW_EPS
        and export_pln >= float(PLANNER_EXPORT_PROFIT_MIN_PLN)
    ):
        return "export_profit"
    if net > _FLOW_EPS and export_pln > 0.0:
        return "export_pv_surplus"
    if abs(ch) <= _FLOW_EPS and abs(dis) <= _FLOW_EPS:
        if net < -_FLOW_EPS:
            return "import_grid"
        return "neutral"
    if ch > _FLOW_EPS and ch_grid <= _FLOW_EPS:
        return "neutral"
    if dis > _FLOW_EPS:
        return "neutral"
    if net < -_FLOW_EPS:
        return "import_grid"
    return "neutral"


def _solve_ecoslot_milp(
    hours_in: list[HourInputs],
    scenarios: list[PlanningScenario],
    *,
    soc_start_pct: float,
    params: BatteryParams,
) -> tuple[np.ndarray, EcoslotOptimizeMeta] | None:
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    wear_per_dis = cycle_cost if cycle_cost > 0.0 else 0.0
    n_h = len(hours_in)
    n_s = len(scenarios)
    if n_h == 0 or n_s == 0:
        return None

    n_vars, layout = _var_layout(n_s, n_h)
    soc_idx = layout["soc_idx"]
    ch_pv_idx = layout["ch_pv_idx"]
    ch_grid_idx = layout["ch_grid_idx"]
    dis_idx = layout["dis_idx"]
    imp_idx = layout["imp_idx"]
    exp_idx = layout["exp_idx"]
    b_cg_idx = layout["b_cg_idx"]
    b_exp_idx = layout["b_exp_idx"]
    z_idx = layout["z_idx"]
    s_base = base_scenario_index(scenarios)
    cheap_import = _cheap_import_threshold(hours_in)

    big_m = _big_m(hours_in, params)
    c = np.zeros(n_vars)

    for h in range(n_h):
        c[ch_pv_idx(h)] += _SIMULTANEOUS_PENALTY
        c[ch_grid_idx(h)] += _SIMULTANEOUS_PENALTY
        c[dis_idx(h)] += _SIMULTANEOUS_PENALTY + wear_per_dis

    for s, sc in enumerate(scenarios):
        pi = float(sc.weight)
        for h, hin in enumerate(hours_in):
            c[imp_idx(s, h)] += pi * hin.import_pln_per_kwh
            c[exp_idx(s, h)] -= pi * hin.export_pln_per_kwh

    eq_rows: list[np.ndarray] = []
    eq_rhs: list[float] = []
    ineq_rows: list[np.ndarray] = []
    ineq_lb: list[float] = []
    ineq_ub: list[float] = []

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
        row[ch_pv_idx(h)] = -eta
        row[ch_grid_idx(h)] = -eta
        row[dis_idx(h)] = 1.0 / eta
        eq_rows.append(row)
        eq_rhs.append(0.0)

    for s, sc in enumerate(scenarios):
        for h in range(n_h):
            row = np.zeros(n_vars)
            row[dis_idx(h)] = 1.0
            row[ch_pv_idx(h)] = -1.0
            row[ch_grid_idx(h)] = -1.0
            row[imp_idx(s, h)] = 1.0
            row[exp_idx(s, h)] = -1.0
            eq_rows.append(row)
            eq_rhs.append(float(sc.load_kwh[h]) - float(sc.pv_kwh[h]))

            row = np.zeros(n_vars)
            row[ch_pv_idx(h)] = 1.0
            ineq_rows.append(row)
            ineq_lb.append(-np.inf)
            ineq_ub.append(float(sc.pv_kwh[h]))

    for h in range(n_h):
        row = np.zeros(n_vars)
        row[ch_grid_idx(h)] = 1.0
        row[b_cg_idx(h)] = -big_m
        ineq_rows.append(row)
        ineq_lb.append(-np.inf)
        ineq_ub.append(0.0)

        row = np.zeros(n_vars)
        row[b_cg_idx(h)] = 1.0
        row[ch_grid_idx(h)] = -1.0 / _FLOW_EPS
        ineq_rows.append(row)
        ineq_lb.append(-np.inf)
        ineq_ub.append(0.0)

        row = np.zeros(n_vars)
        row[b_cg_idx(h)] = 1.0
        row[b_exp_idx(h)] = 1.0
        ineq_rows.append(row)
        ineq_lb.append(-np.inf)
        ineq_ub.append(1.0)

        row = np.zeros(n_vars)
        row[dis_idx(h)] = 1.0
        row[b_cg_idx(h)] = big_m
        ineq_rows.append(row)
        ineq_lb.append(-np.inf)
        ineq_ub.append(big_m)

        row = np.zeros(n_vars)
        row[ch_pv_idx(h)] = 1.0
        row[ch_grid_idx(h)] = 1.0
        row[b_exp_idx(h)] = big_m
        ineq_rows.append(row)
        ineq_lb.append(-np.inf)
        ineq_ub.append(big_m)

        hin = hours_in[h]
        if hin.import_pln_per_kwh > cheap_import:
            row = np.zeros(n_vars)
            row[ch_grid_idx(h)] = 1.0
            ineq_rows.append(row)
            ineq_lb.append(-np.inf)
            ineq_ub.append(0.0)
            row = np.zeros(n_vars)
            row[b_cg_idx(h)] = 1.0
            ineq_rows.append(row)
            ineq_lb.append(-np.inf)
            ineq_ub.append(0.0)

        serve_base = max(
            0.0,
            float(scenarios[s_base].load_kwh[h]) - float(scenarios[s_base].pv_kwh[h]),
        )
        row = np.zeros(n_vars)
        row[dis_idx(h)] = 1.0
        row[b_exp_idx(h)] = -big_m
        ineq_rows.append(row)
        ineq_lb.append(-np.inf)
        ineq_ub.append(serve_base + _SERVE_DISCHARGE_SLACK_KWH + big_m)

        row = np.zeros(n_vars)
        row[b_exp_idx(h)] = 1.0
        row[z_idx(s_base, h)] = -1.0
        ineq_rows.append(row)
        ineq_lb.append(-np.inf)
        ineq_ub.append(0.0)

        row = np.zeros(n_vars)
        row[exp_idx(s_base, h)] = -1.0
        row[b_exp_idx(h)] = _FLOW_EPS
        ineq_rows.append(row)
        ineq_lb.append(-np.inf)
        ineq_ub.append(0.0)

    for s in range(n_s):
        for h in range(n_h):
            row = np.zeros(n_vars)
            row[imp_idx(s, h)] = 1.0
            row[z_idx(s, h)] = big_m
            ineq_rows.append(row)
            ineq_lb.append(-np.inf)
            ineq_ub.append(big_m)

            row = np.zeros(n_vars)
            row[exp_idx(s, h)] = 1.0
            row[z_idx(s, h)] = -big_m
            ineq_rows.append(row)
            ineq_lb.append(-np.inf)
            ineq_ub.append(0.0)

    eq_constraint = LinearConstraint(np.vstack(eq_rows), eq_rhs, eq_rhs)
    ineq_constraint = LinearConstraint(
        np.vstack(ineq_rows),
        np.array(ineq_lb),
        np.array(ineq_ub),
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
        ub[ch_pv_idx(h)] = p_h
        ub[ch_grid_idx(h)] = p_h
        ub[dis_idx(h)] = p_h
        lb[b_cg_idx(h)] = 0.0
        ub[b_cg_idx(h)] = 1.0
        lb[b_exp_idx(h)] = 0.0
        ub[b_exp_idx(h)] = 1.0
    for s in range(n_s):
        for h in range(n_h):
            lb[z_idx(s, h)] = 0.0
            ub[z_idx(s, h)] = 1.0

    integrality = np.zeros(n_vars, dtype=np.int8)
    for h in range(n_h):
        integrality[b_cg_idx(h)] = 1
        integrality[b_exp_idx(h)] = 1
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
        log.warning("ecoslot MILP failed: %s", res.message)
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
                float(x[ch_pv_idx(h)]) + float(x[ch_grid_idx(h)]),
                float(x[dis_idx(h)]),
                cycle_cost_pln=cycle_cost,
            )
            for h in range(n_h)
        )
        scenario_cf.append(grid - wear)

    expected = sum(sc.weight * cf for sc, cf in zip(scenarios, scenario_cf, strict=True))
    meta = EcoslotOptimizeMeta(
        scenarios=scenarios,
        expected_cashflow_pln=expected,
        scenario_cashflow_pln=scenario_cf,
    )
    return x, meta


def _meta_dict(meta: EcoslotOptimizeMeta) -> dict:
    return {
        "model": "ecoslot_shared_ch_dis",
        "expected_cashflow_pln": meta.expected_cashflow_pln,
        "scenario_cashflow_pln": {
            sc.name: cf for sc, cf in zip(meta.scenarios, meta.scenario_cashflow_pln, strict=True)
        },
        "scenario_weights": {sc.name: sc.weight for sc in meta.scenarios},
    }


def _optimize_from_deterministic_fallback(
    hours_in: list[HourInputs],
    *,
    soc_start_pct: float,
    params: BatteryParams,
    reason: str,
) -> OptimizeResult:
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    solved = _solve_milp(hours_in, soc_start_pct=soc_start_pct, params=params)
    if solved is None:
        log.error("ecoslot optimizer: deterministic MILP failed after %s", reason)
        return _fallback_neutral(hours_in, soc_start_pct, params)

    log.warning("ecoslot optimizer: %s — fallback deterministic p50", reason)
    x, total_cf = solved
    from planner.optimizer import _var_layout

    n_h = len(hours_in)
    _, ol = _var_layout(n_h)
    hour_idx = ol["hour_idx"]
    plans: list[HourPlan] = []
    traj: list[float] = [_soc_pct(float(x[0]), params)]
    for h, hin in enumerate(hours_in):
        soc_start = _soc_pct(float(x[h]), params)
        imp = float(x[hour_idx(h, ol["imp"])])
        exp = float(x[hour_idx(h, ol["exp"])])
        ch = float(x[hour_idx(h, ol["ch"])])
        dis = float(x[hour_idx(h, ol["dis"])])
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
        scenario_meta={"ecoslot_milp_failed": True, "fallback": "deterministic_p50", "reason": reason},
    )


def optimize_horizon_ecoslot(
    hours_in: list[HourInputs],
    *,
    soc_start_pct: float,
    params: BatteryParams | None = None,
) -> OptimizeResult:
    """Eco-slot stochastic MILP — wspólna bateria, per-scenariusz bilans sieci."""
    bp = params or BatteryParams()
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    if not hours_in:
        return OptimizeResult(hours=[], total_cashflow_pln=0.0, soc_trajectory_pct=[soc_start_pct])

    scenarios = build_ecoslot_scenarios(hours_in)
    solved = _solve_ecoslot_milp(
        hours_in,
        scenarios,
        soc_start_pct=soc_start_pct,
        params=bp,
    )
    if solved is None:
        return _optimize_from_deterministic_fallback(
            hours_in,
            soc_start_pct=soc_start_pct,
            params=bp,
            reason="ecoslot MILP infeasible/unbounded",
        )

    x, meta = solved
    n_h = len(hours_in)
    n_s = len(scenarios)
    _, layout = _var_layout(n_s, n_h)
    soc_idx = layout["soc_idx"]
    ch_pv_idx = layout["ch_pv_idx"]
    ch_grid_idx = layout["ch_grid_idx"]
    dis_idx = layout["dis_idx"]
    imp_idx = layout["imp_idx"]
    exp_idx = layout["exp_idx"]
    b_cg_idx = layout["b_cg_idx"]
    b_exp_idx = layout["b_exp_idx"]
    s_base = base_scenario_index(scenarios)

    plans: list[HourPlan] = []
    traj: list[float] = [_soc_pct(float(x[soc_idx(0)]), bp)]

    for h, hin in enumerate(hours_in):
        soc_start = _soc_pct(float(x[soc_idx(h)]), bp)
        ch_pv = float(x[ch_pv_idx(h)])
        ch_grid = float(x[ch_grid_idx(h)])
        dis = float(x[dis_idx(h)])
        ch = ch_pv + ch_grid
        imp = float(x[imp_idx(s_base, h)])
        exp = float(x[exp_idx(s_base, h)])
        net = exp - imp
        bd = battery_delta_from_net(pv_kwh=hin.pv_kwh, load_kwh=hin.load_kwh, net_kwh=net)
        soc_end = _soc_pct(float(x[soc_idx(h + 1)]), bp)
        b_cg = float(x[b_cg_idx(h)])
        b_exp = float(x[b_exp_idx(h)])
        exec_mode = _infer_exec_mode(
            ch_pv=ch_pv,
            ch_grid=ch_grid,
            dis=dis,
            net=net,
            export_pln=hin.export_pln_per_kwh,
            load_kwh=hin.load_kwh,
            pv_kwh=hin.pv_kwh,
            b_cg=b_cg,
            b_exp=b_exp,
        )
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
                ch_pv_kwh=ch_pv,
                ch_grid_kwh=ch_grid,
                planned_exec_mode=exec_mode,
            )
        )
        traj.append(soc_end)

    meta_dict = _meta_dict(meta)
    log.info(
        "ecoslot MILP solved: E[cashflow]=%.2f scenarios=%s",
        meta.expected_cashflow_pln,
        meta_dict["scenario_cashflow_pln"],
    )
    return OptimizeResult(
        hours=plans,
        total_cashflow_pln=meta.expected_cashflow_pln,
        soc_trajectory_pct=traj,
        scenario_meta=meta_dict,
    )
