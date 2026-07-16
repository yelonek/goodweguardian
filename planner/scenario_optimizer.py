"""MILP wieloscenariuszowy: wspólne ch/dis/soc (non-anticipativity), sieć recourse per scenariusz, max ważonego E[cashflow]."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from economics import battery_wear_pln_for_hour, cashflow_pln_for_hour
from planner.battery import BatteryParams, max_power_for_hour, soc_kwh
from planner.config import PLANNER_BATTERY_CYCLE_COST_PLN
from planner.hour_remainder import remaining_battery_delta_kwh
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


def _shared_block_size(n_hours: int) -> int:
    """Wspólne sterowanie baterią: soc[H+1] + ch[H] + dis[H]."""
    return 3 * n_hours + 1


def _scenario_var_layout(n_scenarios: int, n_hours: int) -> tuple[int, dict]:
    """
    Non-anticipativity: jedna wspólna trajektoria baterii, sieć jako recourse per scenariusz.

    Zmienne wspólne (jedna decyzja dla wszystkich scenariuszy):
        soc[0..H], ch[0..H-1], dis[0..H-1].
    Zmienne recourse per scenariusz ``s`` (blok po ``3·H``):
        imp[s,h], exp[s,h], z[s,h].
    """
    shared = _shared_block_size(n_hours)

    def soc_idx(h: int) -> int:
        return h

    def ch_idx(h: int) -> int:
        return (n_hours + 1) + h

    def dis_idx(h: int) -> int:
        return (n_hours + 1) + n_hours + h

    def scenario_base(s: int) -> int:
        return shared + s * (3 * n_hours)

    def imp_idx(s: int, h: int) -> int:
        return scenario_base(s) + 3 * h

    def exp_idx(s: int, h: int) -> int:
        return imp_idx(s, h) + 1

    def z_idx(s: int, h: int) -> int:
        return imp_idx(s, h) + 2

    n_vars = shared + n_scenarios * (3 * n_hours)
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
    """max Σ_s π_s·cashflow_s (sieć per scenariusz − wspólny wear baterii)."""
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

    # Sieć: recourse per scenariusz, ważona π_s.
    for s, sc in enumerate(scenarios):
        pi = float(sc.weight)
        for h, hin in enumerate(hours_in):
            c[imp_idx(s, h)] += pi * hin.import_pln_per_kwh
            c[exp_idx(s, h)] -= pi * hin.export_pln_per_kwh
    # Bateria: wspólne sterowanie (Σπ_s = 1) → wear/penalty liczone raz.
    for h in range(n_h):
        c[ch_idx(h)] += _SIMULTANEOUS_PENALTY
        c[dis_idx(h)] += _SIMULTANEOUS_PENALTY + wear_per_dis

    eq_rows: list[np.ndarray] = []
    eq_rhs: list[float] = []
    soc0 = soc_kwh(soc_start_pct, params)
    # params.eta = η_rt; w SOC: +√η·ch − dis/√η (cykl AC→AC = η_rt).
    eta1 = params.eta_one_way

    # Wspólna trajektoria SOC (jedna dla wszystkich scenariuszy).
    row = np.zeros(n_vars)
    row[soc_idx(0)] = 1.0
    eq_rows.append(row)
    eq_rhs.append(soc0)

    for h in range(n_h):
        row = np.zeros(n_vars)
        row[soc_idx(h)] = -1.0
        row[soc_idx(h + 1)] = 1.0
        row[ch_idx(h)] = -eta1
        row[dis_idx(h)] = 1.0 / eta1
        eq_rows.append(row)
        eq_rhs.append(0.0)

    # Bilans per scenariusz — dzielone ch/dis, recourse imp/exp.
    for s in range(n_s):
        sc = scenarios[s]
        for h in range(n_h):
            row = np.zeros(n_vars)
            row[dis_idx(h)] = 1.0
            row[imp_idx(s, h)] = 1.0
            row[ch_idx(h)] = -1.0
            row[exp_idx(s, h)] = -1.0
            eq_rows.append(row)
            # Scenariusz: load/pv full; so_far/N0 z hours_in (wspólne, już zaszłe).
            hin = hours_in[h]
            load_so = float(hin.load_so_far_kwh or 0.0)
            pv_so = float(hin.pv_so_far_kwh or 0.0)
            n0 = float(hin.net_so_far_kwh or 0.0)
            load_rem = float(sc.load_kwh[h]) - load_so
            pv_rem = float(sc.pv_kwh[h]) - pv_so
            eq_rhs.append(load_rem - pv_rem - n0)

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
        constraints=[eq_constraint, exclusivity_constraint],
    )
    if not res.success:
        log.warning("scenario MILP failed: %s", res.message)
        return None

    x = res.x
    # Wear liczony raz na wspólnym sterowaniu — ten sam dla każdego scenariusza.
    shared_wear = sum(
        battery_wear_pln_for_hour(
            float(x[ch_idx(h)]),
            float(x[dis_idx(h)]),
            cycle_cost_pln=cycle_cost,
        )
        for h in range(n_h)
    )
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
        scenario_cf.append(grid - shared_wear)

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
        bd = remaining_battery_delta_kwh(hin, net)
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
        "model": "shared_battery_grid_recourse",
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

    Wspólna trajektoria baterii ch/dis/soc (jedna decyzja dla wszystkich scenariuszy);
    sieć imp/exp/z jako recourse per scenariusz. Miernik netto/cashflow per godzina
    raportowany ze scenariusza bazowego (p50); total = ważona wartość oczekiwana.
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
    traj: list[float] = [_soc_pct(float(x[soc_idx(0)]), bp)]

    for h, hin in enumerate(hours_in):
        soc_start = _soc_pct(float(x[soc_idx(h)]), bp)
        imp = float(x[imp_idx(s_base, h)])
        exp = float(x[exp_idx(s_base, h)])
        ch = float(x[ch_idx(h)])
        dis = float(x[dis_idx(h)])
        net = exp - imp
        bd = remaining_battery_delta_kwh(hin, net)
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
