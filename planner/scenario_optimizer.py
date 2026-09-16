"""MILP wieloscenariuszowy: jedna bateria (shared ch/dis/soc), max E[cashflow] po siatce 5×5."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from economics import battery_wear_pln_for_hour, cashflow_pln_for_hour
from planner.battery import (
    BatteryParams,
    effective_soc_floor_kwh,
    max_power_for_hour,
    soc_kwh,
)
from planner.config import PLANNER_BATTERY_CYCLE_COST_PLN
from planner.hour_remainder import (
    green_export_cap_rhs_kwh,
    meter_export_so_far_kwh,
    pv_remainder_kwh,
    remaining_battery_delta_kwh,
)
from planner.models import HourInputs, HourPlan, ScenarioSeriesDetail, ScenariosDetail
from planner.night_grid_policy import (
    add_green_stock_constraints,
    add_night_latch_constraints,
    green_stock_n_vars,
    green_var_indexers,
    night_latch_layout,
    resolve_green0_kwh,
)
from planner.optimizer import OptimizeResult, _big_m, _soc_pct, _solve_milp
from planner.scenarios import (
    PlanningScenario,
    build_planning_scenarios,
    representative_scenario_index,
)

log = logging.getLogger("planner")


@dataclass
class ScenarioOptimizeMeta:
    """Metadane solve — do audytu / debug."""

    scenarios: list[PlanningScenario]
    expected_cashflow_pln: float
    scenario_cashflow_pln: list[float]
    model: str = "shared_battery_grid_recourse"


# ---------------------------------------------------------------------------
# Shared ch/dis/soc, grid recourse per scenariusz (5×5)
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
    night_charge_carry_in: bool = False,
    green_stock_kwh: float | None = None,
) -> tuple[np.ndarray, ScenarioOptimizeMeta] | None:
    """Wspólne ch/dis/soc, sieć (imp/exp) per scenariusz; max Σ π_s CF_s."""
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    wear_per_dis = cycle_cost if cycle_cost > 0.0 else 0.0
    n_h = len(hours_in)
    n_s = len(scenarios)
    if n_h == 0 or n_s == 0:
        return None

    n_vars_core, layout = _shared_var_layout(n_s, n_h)
    soc_idx = layout["soc_idx"]
    ch_idx = layout["ch_idx"]
    dis_idx = layout["dis_idx"]
    z_idx = layout["z_idx"]
    imp_idx = layout["imp_idx"]
    exp_idx = layout["exp_idx"]
    _windows, night_pos = night_latch_layout(hours_in)
    n_night = len(night_pos)
    n_green = green_stock_n_vars(n_h)
    n_vars = n_vars_core + 2 * n_night + n_green

    def y_ch_idx(h: int) -> int:
        return n_vars_core + night_pos[h]

    def latch_idx(h: int) -> int:
        return n_vars_core + n_night + night_pos[h]

    green_index, ch_pv_index, dis_g_index = green_var_indexers(
        n_vars_core + 2 * n_night, n_h
    )

    big_m = _big_m(hours_in, params)
    c = np.zeros(n_vars)

    for s, sc in enumerate(scenarios):
        pi = float(sc.weight)
        for h, hin in enumerate(hours_in):
            c[imp_idx(s, h)] += pi * hin.import_pln_per_kwh
            c[exp_idx(s, h)] -= pi * hin.export_pln_per_kwh
    for h in range(n_h):
        c[dis_idx(h)] += wear_per_dis

    eq_rows: list[np.ndarray] = []
    eq_rhs: list[float] = []
    soc0 = soc_kwh(soc_start_pct, params)
    eta1 = params.eta_one_way
    soc_floor = effective_soc_floor_kwh(soc_start_pct, params)
    soc_max = soc_kwh(params.soc_max_pct, params)
    green0 = resolve_green0_kwh(green_stock_kwh, soc0)
    lb = np.zeros(n_vars)
    ub = np.full(n_vars, np.inf)

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
        soc_index=soc_idx,
        ch_index=ch_idx,
        dis_index=dis_idx,
        exp_index=None,
        green_index=green_index,
        ch_pv_index=ch_pv_index,
        dis_g_index=dis_g_index,
        pv_kwh_of=lambda h: pv_remainder_kwh(hours_in[h]),
        pmax_of=lambda h: max_power_for_hour(hours_in[h], params),
        lb=lb,
        ub=ub,
        export_already_kwh_of=lambda h: meter_export_so_far_kwh(hours_in[h]),
    )
    for s, sc in enumerate(scenarios):
        for h in range(n_h):
            row = np.zeros(n_vars)
            row[exp_idx(s, h)] = 1.0
            row[dis_g_index(h)] = -1.0
            ineq_rows.append(row)
            ineq_rhs.append(
                green_export_cap_rhs_kwh(hours_in[h], pv_kwh=float(sc.pv_kwh[h]))
            )

    eq_constraint = LinearConstraint(np.vstack(eq_rows), eq_rhs, eq_rhs)

    for s in range(n_s):
        for h in range(n_h):
            row = np.zeros(n_vars)
            row[imp_idx(s, h)] = 1.0
            row[z_idx(s, h)] = big_m
            ineq_rows.append(row)
            ineq_rhs.append(big_m)

            row = np.zeros(n_vars)
            row[exp_idx(s, h)] = 1.0
            row[z_idx(s, h)] = -big_m
            ineq_rows.append(row)
            ineq_rhs.append(0.0)

    if n_night > 0:
        for s in range(n_s):
            add_night_latch_constraints(
                ineq_rows,
                ineq_rhs,
                n_vars=n_vars,
                hours_in=hours_in,
                ch_index=ch_idx,
                exp_index=lambda h, s=s: exp_idx(s, h),
                y_ch_index=y_ch_idx,
                latch_index=latch_idx,
                pmax_of=lambda h: max_power_for_hour(hours_in[h], params),
                big_m=big_m,
                carry_in=night_charge_carry_in,
            )

    exclusivity_constraint = LinearConstraint(
        np.vstack(ineq_rows),
        -np.full(len(ineq_rhs), np.inf),
        np.array(ineq_rhs),
    )

    for h in range(n_h + 1):
        lb[soc_idx(h)] = soc_floor
        ub[soc_idx(h)] = soc_max
    for h in range(n_h):
        p_h = max_power_for_hour(hours_in[h], params)
        ub[ch_idx(h)] = p_h
        ub[dis_idx(h)] = p_h
    for h in night_pos:
        lb[y_ch_idx(h)] = 0.0
        ub[y_ch_idx(h)] = 1.0
        lb[latch_idx(h)] = 0.0
        ub[latch_idx(h)] = 1.0
    for s in range(n_s):
        for h in range(n_h):
            lb[z_idx(s, h)] = 0.0
            ub[z_idx(s, h)] = 1.0

    integrality = np.zeros(n_vars, dtype=np.int8)
    for s in range(n_s):
        for h in range(n_h):
            integrality[z_idx(s, h)] = 1
    for h in night_pos:
        integrality[y_ch_idx(h)] = 1
        integrality[latch_idx(h)] = 1

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
    night_charge_carry_in: bool = False,
    green_stock_kwh: float | None = None,
) -> OptimizeResult:
    """Fallback: deterministyczny MILP (p50)."""
    from planner.optimizer import _var_layout

    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    solved = _solve_milp(
        hours_in,
        soc_start_pct=soc_start_pct,
        params=params,
        night_charge_carry_in=night_charge_carry_in,
        green_stock_kwh=green_stock_kwh,
    )
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
    return out


def _slots_from_hours(hours_in: list[HourInputs]) -> list[dict]:
    return [{"date": hin.date, "hour": hin.hour} for hin in hours_in]


def _result_from_shared(
    x: np.ndarray,
    meta: ScenarioOptimizeMeta,
    hours_in: list[HourInputs],
    scenarios: list[PlanningScenario],
    params: BatteryParams,
) -> OptimizeResult:
    cycle_cost = float(PLANNER_BATTERY_CYCLE_COST_PLN)
    n_h = len(hours_in)
    _, layout = _shared_var_layout(len(scenarios), n_h)
    soc_idx = layout["soc_idx"]
    ch_idx = layout["ch_idx"]
    dis_idx = layout["dis_idx"]
    imp_idx = layout["imp_idx"]
    exp_idx = layout["exp_idx"]
    s_rep = representative_scenario_index(scenarios)

    plans: list[HourPlan] = []
    traj: list[float] = [_soc_pct(float(x[soc_idx(0)]), params)]

    for h, hin in enumerate(hours_in):
        soc_start = _soc_pct(float(x[soc_idx(h)]), params)
        imp = float(x[imp_idx(s_rep, h)])
        exp = float(x[exp_idx(s_rep, h)])
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

    # Wspólny SOC; net/CF różnią się per świat.
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
    night_charge_carry_in: bool = False,
    green_stock_kwh: float | None = None,
) -> OptimizeResult:
    """Jedna trajektoria baterii: max E[cashflow] po 25 niezależnych światach 5×5."""
    bp = params or BatteryParams()
    if not hours_in:
        return OptimizeResult(hours=[], total_cashflow_pln=0.0, soc_trajectory_pct=[soc_start_pct])

    scenarios = build_planning_scenarios(hours_in)
    solved = _solve_shared_milp(
        hours_in,
        scenarios,
        soc_start_pct=soc_start_pct,
        params=bp,
        night_charge_carry_in=night_charge_carry_in,
        green_stock_kwh=green_stock_kwh,
    )
    if solved is None:
        return _optimize_from_deterministic_milp(
            hours_in,
            soc_start_pct=soc_start_pct,
            params=bp,
            reason="shared-battery MILP infeasible/unbounded",
            night_charge_carry_in=night_charge_carry_in,
            green_stock_kwh=green_stock_kwh,
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
