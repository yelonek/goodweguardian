"""Scenariusze PV/load dla eco-slot stochastic MILP."""

from __future__ import annotations

from planner.config import (
    PLANNER_ECOSLOT_SCENARIO_GRID,
    PLANNER_SCENARIO_WEIGHT_BASE,
    PLANNER_SCENARIO_WEIGHT_OPTIMISTIC,
    PLANNER_SCENARIO_WEIGHT_PESSIMISTIC,
)
from planner.models import HourInputs
from planner.scenarios import PlanningScenario, _pv_band


def _load_band(hin: HourInputs, which: str) -> float:
    if which == "p75":
        return float(hin.load_kwh_p75 if hin.load_kwh_p75 is not None else hin.load_kwh)
    if which == "p25":
        return float(hin.load_kwh_p25 if hin.load_kwh_p25 is not None else hin.load_kwh)
    return float(hin.load_kwh)


def _normalize_weights(weights: list[float]) -> list[float]:
    total = sum(weights)
    if total <= 0.0:
        n = len(weights)
        return [1.0 / n] * n if n else []
    return [w / total for w in weights]


def build_ecoslot_scenarios(hours_in: list[HourInputs]) -> list[PlanningScenario]:
    """
  Scenariusze na cały horyzont.

  ``PLANNER_ECOSLOT_SCENARIO_GRID=3``: pesymistyczny (p10,p75), baza (p50,p50),
  optymistyczny (p90,p25).
  ``=9``: pełna siatka PV p10/p50/p90 × load p25/p50/p75 (wagi równe).
  """
    if not hours_in:
        return []

    grid = int(PLANNER_ECOSLOT_SCENARIO_GRID)
    if grid == 9:
        return _build_grid_9(hours_in)
    return _build_grid_3(hours_in)


def _build_grid_3(hours_in: list[HourInputs]) -> list[PlanningScenario]:
    pess_pv, pess_load = [], []
    base_pv, base_load = [], []
    opt_pv, opt_load = [], []
    for hin in hours_in:
        pess_pv.append(_pv_band(hin, "p10"))
        pess_load.append(_load_band(hin, "p75"))
        base_pv.append(_pv_band(hin, "p50"))
        base_load.append(_load_band(hin, "p50"))
        opt_pv.append(_pv_band(hin, "p90"))
        opt_load.append(_load_band(hin, "p25"))

    w_p, w_b, w_o = _normalize_weights(
        [
            float(PLANNER_SCENARIO_WEIGHT_PESSIMISTIC),
            float(PLANNER_SCENARIO_WEIGHT_BASE),
            float(PLANNER_SCENARIO_WEIGHT_OPTIMISTIC),
        ]
    )
    return [
        PlanningScenario("pessimistic", w_p, tuple(pess_pv), tuple(pess_load)),
        PlanningScenario("base", w_b, tuple(base_pv), tuple(base_load)),
        PlanningScenario("optimistic", w_o, tuple(opt_pv), tuple(opt_load)),
    ]


def _build_grid_9(hours_in: list[HourInputs]) -> list[PlanningScenario]:
    pv_bands = [("p10", "p10"), ("p50", "p50"), ("p90", "p90")]
    load_bands = [("p25", "p25"), ("p50", "p50"), ("p75", "p75")]
    scenarios: list[PlanningScenario] = []
    for pv_name, pv_key in pv_bands:
        for ld_name, ld_key in load_bands:
            pv_t, ld_t = [], []
            for hin in hours_in:
                pv_t.append(_pv_band(hin, pv_key))
                ld_t.append(_load_band(hin, ld_key))
            scenarios.append(
                PlanningScenario(
                    f"pv_{pv_name}_load_{ld_name}",
                    1.0,
                    tuple(pv_t),
                    tuple(ld_t),
                )
            )
    weights = _normalize_weights([1.0] * len(scenarios))
    return [
        PlanningScenario(sc.name, w, sc.pv_kwh, sc.load_kwh)
        for sc, w in zip(scenarios, weights, strict=True)
    ]


def base_scenario_index(scenarios: list[PlanningScenario]) -> int:
    for i, sc in enumerate(scenarios):
        if sc.name == "base":
            return i
    for i, sc in enumerate(scenarios):
        if "p50" in sc.name and sc.name.count("p50") >= 1:
            if sc.name in ("pv_p50_load_p50", "base"):
                return i
    mid = len(scenarios) // 2
    return mid
