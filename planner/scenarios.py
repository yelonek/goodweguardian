"""Scenariusze prognozy PV/load dla optymalizatora ryzyka."""

from __future__ import annotations

from dataclasses import dataclass

from planner.config import (
    PLANNER_SCENARIO_WEIGHT_BASE,
    PLANNER_SCENARIO_WEIGHT_OPTIMISTIC,
    PLANNER_SCENARIO_WEIGHT_PESSIMISTIC,
)
from planner.models import HourInputs


@dataclass(frozen=True)
class PlanningScenario:
    """Jeden profil PV/load na cały horyzont."""

    name: str
    weight: float
    pv_kwh: tuple[float, ...]
    load_kwh: tuple[float, ...]


def _pv_band(hin: HourInputs, which: str) -> float:
    if which == "p10":
        return float(hin.pv_kwh_p10 if hin.pv_kwh_p10 is not None else hin.pv_kwh)
    if which == "p90":
        return float(hin.pv_kwh_p90 if hin.pv_kwh_p90 is not None else hin.pv_kwh)
    return float(hin.pv_kwh)


def _load_band(hin: HourInputs, which: str) -> float:
    if which == "p75":
        return float(hin.load_kwh_p75 if hin.load_kwh_p75 is not None else hin.load_kwh)
    if which == "p25":
        return float(hin.load_kwh_p25 if hin.load_kwh_p25 is not None else hin.load_kwh)
    return float(hin.load_kwh)


def build_planning_scenarios(hours_in: list[HourInputs]) -> list[PlanningScenario]:
    """
    Trzy scenariusze: pesymistyczny (PV p10, load p75), bazowy (p50), optymistyczny (PV p90, load p25).
    """
    if not hours_in:
        return []

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

    w_p = float(PLANNER_SCENARIO_WEIGHT_PESSIMISTIC)
    w_b = float(PLANNER_SCENARIO_WEIGHT_BASE)
    w_o = float(PLANNER_SCENARIO_WEIGHT_OPTIMISTIC)
    total = w_p + w_b + w_o
    if total <= 0.0:
        w_p, w_b, w_o = 0.15, 0.70, 0.15
        total = 1.0
    w_p, w_b, w_o = w_p / total, w_b / total, w_o / total

    return [
        PlanningScenario("pessimistic", w_p, tuple(pess_pv), tuple(pess_load)),
        PlanningScenario("base", w_b, tuple(base_pv), tuple(base_load)),
        PlanningScenario("optimistic", w_o, tuple(opt_pv), tuple(opt_load)),
    ]


def base_scenario_index(scenarios: list[PlanningScenario]) -> int:
    for i, sc in enumerate(scenarios):
        if sc.name == "base":
            return i
    return 0
