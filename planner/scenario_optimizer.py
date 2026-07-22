"""MILP wieloscenariuszowy: tracking-SP (SOC* first-stage) lub legacy shared ch/dis."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from economics import battery_wear_pln_for_hour, cashflow_pln_for_hour
from planner.battery import BatteryParams, max_power_for_hour, soc_kwh
from planner.config import (
    PLANNER_BATTERY_CYCLE_COST_PLN,
    PLANNER_SOC_TRACKING_LAMBDA,
    planner_soc_tracking_enabled,
)
from planner.hour_remainder import remaining_battery_delta_kwh
from planner.models import HourInputs, HourPlan, ScenarioSeriesDetail, ScenariosDetail
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
    model: str = "soc_tracking_recourse"
    tracking_penalty_pln: float = 0.0


# ---------------------------------------------------------------------------
# Tracking-SP: soc* first-stage, ch/dis/imp/exp recourse per scenariusz
# ---------------------------------------------------------------------------


def _tracking_var_layout(n_scenarios: int, n_hours: int) -> tuple[int, dict]:
    """
    First-stage: soc_star[0..H].
    Per scenariusz ``s`` (blok ``8·H + 1``):
        soc_s[0..H], (ch, dis, imp, exp, z, dpos, dneg)×H
    ``dpos/dneg`` linearizują |soc_s[h+1] − soc_star[h+1]|.
    """
    n_h = n_hours
    n_s = n_scenarios
    n_star = n_h + 1
    block = 8 * n_h + 1

    def soc_star_idx(h: int) -> int:
        return h

    def scen_base(s: int) -> int:
        return n_star + s * block

    def soc_s_idx(s: int, h: int) -> int:
        return scen_base(s) + h

    def ch_idx(s: int, h: int) -> int:
        return scen_base(s) + (n_h + 1) + h

    def dis_idx(s: int, h: int) -> int:
        return scen_base(s) + (n_h + 1) + n_h + h

    def imp_idx(s: int, h: int) -> int:
        return scen_base(s) + (n_h + 1) + 2 * n_h + h

    def exp_idx(s: int, h: int) -> int:
        return scen_base(s) + (n_h + 1) + 3 * n_h + h

    def z_idx(s: int, h: int) -> int:
        return scen_base(s) + (n_h + 1) + 4 * n_h + h

    def dpos_idx(s: int, h: int) -> int:
        """Odchylenie dodatnie na końcu godziny ``h`` (soc index h+1)."""
        return scen_base(s) + (n_h + 1) + 5 * n_h + h

    def dneg_idx(s: int, h: int) -> int:
        return scen_base(s) + (n_h + 1) + 6 * n_h + h

    n_vars = n_star + n_s * block
    return n_vars, {
        "n_hours": n_h,
        "n_scenarios": n_s,
        "soc_star_idx": soc_star_idx,
        "soc_s_idx": soc_s_idx,
        "ch_idx": ch_idx,
        "dis_idx": dis_idx,
        "imp_idx": imp_idx,
        "exp_idx": exp_idx,
        "z_idx": z_idx,
        "dpos_idx": dpos_idx,
        "dneg_idx": dneg_idx,
    }


def _solve_tracking_milp(
    hours_in: list[HourInputs],
    scenarios: list[PlanningScenario],
    *,
    soc_start_pct: float,
    params: BatteryParams,
    tracking_lambda: float,
) -> tuple[np.ndarray, ScenarioOptimizeMeta] | None:
    """
    max Σ_s π_s · CF_s − λ · Σ_s π_s · Σ_h |soc_s,h − soc*_h|

    ``tracking_lambda`` [PLN/kWh energii magazynu].
    """
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    wear_per_dis = cycle_cost if cycle_cost > 0.0 else 0.0
    lam = max(0.0, float(tracking_lambda))
    n_h = len(hours_in)
    n_s = len(scenarios)
    if n_h == 0 or n_s == 0:
        return None

    n_vars, layout = _tracking_var_layout(n_s, n_h)
    soc_star_idx = layout["soc_star_idx"]
    soc_s_idx = layout["soc_s_idx"]
    ch_idx = layout["ch_idx"]
    dis_idx = layout["dis_idx"]
    imp_idx = layout["imp_idx"]
    exp_idx = layout["exp_idx"]
    z_idx = layout["z_idx"]
    dpos_idx = layout["dpos_idx"]
    dneg_idx = layout["dneg_idx"]

    big_m = _big_m(hours_in, params)
    c = np.zeros(n_vars)
    eta1 = params.eta_one_way
    soc0 = soc_kwh(soc_start_pct, params)
    soc_min = soc_kwh(params.soc_min_pct, params)
    soc_max = soc_kwh(params.soc_max_pct, params)

    for s, sc in enumerate(scenarios):
        pi = float(sc.weight)
        for h, hin in enumerate(hours_in):
            c[imp_idx(s, h)] += pi * hin.import_pln_per_kwh
            c[exp_idx(s, h)] -= pi * hin.export_pln_per_kwh
            c[ch_idx(s, h)] += _SIMULTANEOUS_PENALTY
            c[dis_idx(s, h)] += _SIMULTANEOUS_PENALTY + pi * wear_per_dis
            if lam > 0.0:
                c[dpos_idx(s, h)] += pi * lam
                c[dneg_idx(s, h)] += pi * lam

    eq_rows: list[np.ndarray] = []
    eq_rhs: list[float] = []

    # soc*_0 = soc0
    row = np.zeros(n_vars)
    row[soc_star_idx(0)] = 1.0
    eq_rows.append(row)
    eq_rhs.append(soc0)

    for s in range(n_s):
        # soc_s,0 = soc0
        row = np.zeros(n_vars)
        row[soc_s_idx(s, 0)] = 1.0
        eq_rows.append(row)
        eq_rhs.append(soc0)

        sc = scenarios[s]
        for h in range(n_h):
            hin = hours_in[h]
            # SOC dynamics per scenario
            row = np.zeros(n_vars)
            row[soc_s_idx(s, h)] = -1.0
            row[soc_s_idx(s, h + 1)] = 1.0
            row[ch_idx(s, h)] = -eta1
            row[dis_idx(s, h)] = 1.0 / eta1
            eq_rows.append(row)
            eq_rhs.append(0.0)

            # Power balance
            row = np.zeros(n_vars)
            row[dis_idx(s, h)] = 1.0
            row[imp_idx(s, h)] = 1.0
            row[ch_idx(s, h)] = -1.0
            row[exp_idx(s, h)] = -1.0
            eq_rows.append(row)
            load_so = float(hin.load_so_far_kwh or 0.0)
            pv_so = float(hin.pv_so_far_kwh or 0.0)
            n0 = float(hin.net_so_far_kwh or 0.0)
            load_rem = float(sc.load_kwh[h]) - load_so
            pv_rem = float(sc.pv_kwh[h]) - pv_so
            eq_rhs.append(load_rem - pv_rem - n0)

            # Tracking: soc_s[h+1] - soc*[h+1] = dpos - dneg
            row = np.zeros(n_vars)
            row[soc_s_idx(s, h + 1)] = 1.0
            row[soc_star_idx(h + 1)] = -1.0
            row[dpos_idx(s, h)] = -1.0
            row[dneg_idx(s, h)] = 1.0
            eq_rows.append(row)
            eq_rhs.append(0.0)

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

    lb = np.zeros(n_vars)
    ub = np.full(n_vars, np.inf)
    for h in range(n_h + 1):
        lb[soc_star_idx(h)] = soc_min
        ub[soc_star_idx(h)] = soc_max
    for s in range(n_s):
        for h in range(n_h + 1):
            lb[soc_s_idx(s, h)] = soc_min
            ub[soc_s_idx(s, h)] = soc_max
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
        log.warning("tracking MILP failed: %s", res.message)
        return None

    x = res.x
    scenario_cf: list[float] = []
    tracking_pen = 0.0
    for s, sc in enumerate(scenarios):
        pi = float(sc.weight)
        grid = 0.0
        wear = 0.0
        for h, hin in enumerate(hours_in):
            imp = float(x[imp_idx(s, h)])
            exp = float(x[exp_idx(s, h)])
            ch = float(x[ch_idx(s, h)])
            dis = float(x[dis_idx(s, h)])
            grid += cashflow_pln_for_hour(
                exp - imp,
                rce_pln_per_kwh=hin.export_pln_per_kwh,
                import_pln_per_kwh=hin.import_pln_per_kwh,
            )
            wear += battery_wear_pln_for_hour(ch, dis, cycle_cost_pln=cycle_cost)
            tracking_pen += pi * lam * (
                float(x[dpos_idx(s, h)]) + float(x[dneg_idx(s, h)])
            )
        scenario_cf.append(grid - wear)

    expected = sum(sc.weight * cf for sc, cf in zip(scenarios, scenario_cf, strict=True))
    meta = ScenarioOptimizeMeta(
        scenarios=scenarios,
        expected_cashflow_pln=expected,
        scenario_cashflow_pln=scenario_cf,
        model="soc_tracking_recourse",
        tracking_penalty_pln=tracking_pen,
    )
    return x, meta


# ---------------------------------------------------------------------------
# Legacy: shared ch/dis/soc, grid recourse (pre-tracking)
# ---------------------------------------------------------------------------


def _shared_block_size(n_hours: int) -> int:
    return 3 * n_hours + 1


def _shared_var_layout(n_scenarios: int, n_hours: int) -> tuple[int, dict]:
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


def _solve_shared_milp(
    hours_in: list[HourInputs],
    scenarios: list[PlanningScenario],
    *,
    soc_start_pct: float,
    params: BatteryParams,
) -> tuple[np.ndarray, ScenarioOptimizeMeta] | None:
    """Legacy: wspólne ch/dis/soc, sieć per scenariusz."""
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    wear_per_dis = cycle_cost if cycle_cost > 0.0 else 0.0
    n_h = len(hours_in)
    n_s = len(scenarios)
    if n_h == 0 or n_s == 0:
        return None

    n_vars, layout = _shared_var_layout(n_s, n_h)
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
    for h in range(n_h):
        c[ch_idx(h)] += _SIMULTANEOUS_PENALTY
        c[dis_idx(h)] += _SIMULTANEOUS_PENALTY + wear_per_dis

    eq_rows: list[np.ndarray] = []
    eq_rhs: list[float] = []
    soc0 = soc_kwh(soc_start_pct, params)
    eta1 = params.eta_one_way

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

    for s in range(n_s):
        sc = scenarios[s]
        for h in range(n_h):
            row = np.zeros(n_vars)
            row[dis_idx(h)] = 1.0
            row[imp_idx(s, h)] = 1.0
            row[ch_idx(h)] = -1.0
            row[exp_idx(s, h)] = -1.0
            eq_rows.append(row)
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
        log.warning("shared-battery MILP failed: %s", res.message)
        return None

    x = res.x
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
        model="shared_battery_grid_recourse",
    )
    return x, meta


def _optimize_from_deterministic_milp(
    hours_in: list[HourInputs],
    *,
    soc_start_pct: float,
    params: BatteryParams,
    reason: str,
) -> OptimizeResult:
    """Fallback: deterministyczny MILP (p50)."""
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
    out: dict = {
        "model": meta.model,
        "expected_cashflow_pln": meta.expected_cashflow_pln,
        "scenario_cashflow_pln": {
            sc.name: cf
            for sc, cf in zip(meta.scenarios, meta.scenario_cashflow_pln, strict=True)
        },
        "scenario_weights": {sc.name: sc.weight for sc in meta.scenarios},
    }
    if meta.model == "soc_tracking_recourse":
        out["tracking_penalty_pln"] = meta.tracking_penalty_pln
        out["tracking_lambda"] = float(PLANNER_SOC_TRACKING_LAMBDA)
    return out


def _slots_from_hours(hours_in: list[HourInputs]) -> list[dict]:
    return [{"date": hin.date, "hour": hin.hour} for hin in hours_in]


def _result_from_tracking(
    x: np.ndarray,
    meta: ScenarioOptimizeMeta,
    hours_in: list[HourInputs],
    scenarios: list[PlanningScenario],
    params: BatteryParams,
) -> OptimizeResult:
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    n_h = len(hours_in)
    n_s = len(scenarios)
    _, layout = _tracking_var_layout(n_s, n_h)
    soc_star_idx = layout["soc_star_idx"]
    soc_s_idx = layout["soc_s_idx"]
    ch_idx = layout["ch_idx"]
    dis_idx = layout["dis_idx"]
    imp_idx = layout["imp_idx"]
    exp_idx = layout["exp_idx"]
    s_base = base_scenario_index(scenarios)

    plans: list[HourPlan] = []
    traj: list[float] = [_soc_pct(float(x[soc_star_idx(0)]), params)]

    for h, hin in enumerate(hours_in):
        soc_start = _soc_pct(float(x[soc_star_idx(h)]), params)
        soc_end = _soc_pct(float(x[soc_star_idx(h + 1)]), params)
        imp = float(x[imp_idx(s_base, h)])
        exp = float(x[exp_idx(s_base, h)])
        ch = float(x[ch_idx(s_base, h)])
        dis = float(x[dis_idx(s_base, h)])
        net = exp - imp
        bd = remaining_battery_delta_kwh(hin, net)
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

    scenario_series: dict[str, ScenarioSeriesDetail] = {}
    for s, sc in enumerate(scenarios):
        soc_pct = [_soc_pct(float(x[soc_s_idx(s, h)]), params) for h in range(n_h + 1)]
        net_kwh: list[float] = []
        cf_hour: list[float] = []
        for h, hin in enumerate(hours_in):
            imp = float(x[imp_idx(s, h)])
            exp = float(x[exp_idx(s, h)])
            ch = float(x[ch_idx(s, h)])
            dis = float(x[dis_idx(s, h)])
            net = exp - imp
            grid_cf = cashflow_pln_for_hour(
                net,
                rce_pln_per_kwh=hin.export_pln_per_kwh,
                import_pln_per_kwh=hin.import_pln_per_kwh,
            )
            wear = battery_wear_pln_for_hour(ch, dis, cycle_cost_pln=cycle_cost)
            net_kwh.append(net)
            cf_hour.append(grid_cf - wear)
        scenario_series[sc.name] = ScenarioSeriesDetail(
            weight=float(sc.weight),
            cashflow_pln=float(meta.scenario_cashflow_pln[s]),
            soc_pct=soc_pct,
            net_kwh=net_kwh,
            cashflow_hour_pln=cf_hour,
        )

    detail = ScenariosDetail(
        model=meta.model,
        expected_cashflow_pln=float(meta.expected_cashflow_pln),
        soc_star_pct=list(traj),
        slots=_slots_from_hours(hours_in),
        scenarios=scenario_series,
        tracking_penalty_pln=float(meta.tracking_penalty_pln),
        tracking_lambda=float(PLANNER_SOC_TRACKING_LAMBDA),
    )

    return OptimizeResult(
        hours=plans,
        total_cashflow_pln=meta.expected_cashflow_pln,
        soc_trajectory_pct=traj,
        scenario_meta=_scenario_meta_dict(meta),
        scenarios_detail=detail,
    )


def _result_from_shared(
    x: np.ndarray,
    meta: ScenarioOptimizeMeta,
    hours_in: list[HourInputs],
    scenarios: list[PlanningScenario],
    params: BatteryParams,
) -> OptimizeResult:
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    n_h = len(hours_in)
    n_s = len(scenarios)
    _, layout = _shared_var_layout(n_s, n_h)
    soc_idx = layout["soc_idx"]
    ch_idx = layout["ch_idx"]
    dis_idx = layout["dis_idx"]
    imp_idx = layout["imp_idx"]
    exp_idx = layout["exp_idx"]
    s_base = base_scenario_index(scenarios)

    plans: list[HourPlan] = []
    traj: list[float] = [_soc_pct(float(x[soc_idx(0)]), params)]

    for h, hin in enumerate(hours_in):
        soc_start = _soc_pct(float(x[soc_idx(h)]), params)
        imp = float(x[imp_idx(s_base, h)])
        exp = float(x[exp_idx(s_base, h)])
        ch = float(x[ch_idx(h)])
        dis = float(x[dis_idx(h)])
        net = exp - imp
        bd = remaining_battery_delta_kwh(hin, net)
        soc_end = _soc_pct(float(x[soc_idx(h + 1)]), params)
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

    # Legacy shared SOC — jedna trajektoria; net/CF różnią się per scenariusz.
    shared_wear_by_h = [
        battery_wear_pln_for_hour(
            float(x[ch_idx(h)]),
            float(x[dis_idx(h)]),
            cycle_cost_pln=cycle_cost,
        )
        for h in range(n_h)
    ]
    scenario_series: dict[str, ScenarioSeriesDetail] = {}
    for s, sc in enumerate(scenarios):
        net_kwh: list[float] = []
        cf_hour: list[float] = []
        for h, hin in enumerate(hours_in):
            imp = float(x[imp_idx(s, h)])
            exp = float(x[exp_idx(s, h)])
            net = exp - imp
            grid_cf = cashflow_pln_for_hour(
                net,
                rce_pln_per_kwh=hin.export_pln_per_kwh,
                import_pln_per_kwh=hin.import_pln_per_kwh,
            )
            net_kwh.append(net)
            cf_hour.append(grid_cf - shared_wear_by_h[h])
        scenario_series[sc.name] = ScenarioSeriesDetail(
            weight=float(sc.weight),
            cashflow_pln=float(meta.scenario_cashflow_pln[s]),
            soc_pct=list(traj),
            net_kwh=net_kwh,
            cashflow_hour_pln=cf_hour,
        )

    detail = ScenariosDetail(
        model=meta.model,
        expected_cashflow_pln=float(meta.expected_cashflow_pln),
        soc_star_pct=list(traj),
        slots=_slots_from_hours(hours_in),
        scenarios=scenario_series,
    )

    return OptimizeResult(
        hours=plans,
        total_cashflow_pln=meta.expected_cashflow_pln,
        soc_trajectory_pct=traj,
        scenario_meta=_scenario_meta_dict(meta),
        scenarios_detail=detail,
    )


def optimize_horizon_scenarios(
    hours_in: list[HourInputs],
    *,
    soc_start_pct: float,
    params: BatteryParams | None = None,
) -> OptimizeResult:
    """
    Wieloscenariuszowy MILP: max ważonego E[cashflow].

    Domyślnie **tracking-SP**: wspólna wizja ``soc*``, recourse ``ch/dis/imp/exp``
    per scenariusz, kara ``λ·|SOC_s−SOC*|``.

    ``planner_soc_tracking=false`` → legacy shared ch/dis/soc.
    """
    bp = params or BatteryParams()
    if not hours_in:
        return OptimizeResult(hours=[], total_cashflow_pln=0.0, soc_trajectory_pct=[soc_start_pct])

    scenarios = build_planning_scenarios(hours_in)
    use_tracking = planner_soc_tracking_enabled()

    if use_tracking:
        solved = _solve_tracking_milp(
            hours_in,
            scenarios,
            soc_start_pct=soc_start_pct,
            params=bp,
            tracking_lambda=float(PLANNER_SOC_TRACKING_LAMBDA),
        )
        if solved is None:
            return _optimize_from_deterministic_milp(
                hours_in,
                soc_start_pct=soc_start_pct,
                params=bp,
                reason="tracking MILP infeasible/unbounded",
            )
        x, meta = solved
        log.info(
            "tracking MILP solved: E[cashflow]=%.2f penalty=%.2f scenarios=%s",
            meta.expected_cashflow_pln,
            meta.tracking_penalty_pln,
            {
                sc.name: cf
                for sc, cf in zip(meta.scenarios, meta.scenario_cashflow_pln, strict=True)
            },
        )
        return _result_from_tracking(x, meta, hours_in, scenarios, bp)

    solved = _solve_shared_milp(
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
            reason="shared-battery MILP infeasible/unbounded",
        )
    x, meta = solved
    log.info(
        "shared-battery MILP solved: E[cashflow]=%.2f scenarios=%s",
        meta.expected_cashflow_pln,
        {
            sc.name: cf
            for sc, cf in zip(meta.scenarios, meta.scenario_cashflow_pln, strict=True)
        },
    )
    return _result_from_shared(x, meta, hours_in, scenarios, bp)
