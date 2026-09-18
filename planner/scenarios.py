"""Siatka 5×5 niezależnych światów PV×load (kwantyle 10/30/50/70/90)."""

from __future__ import annotations

from dataclasses import dataclass

from planner.models import HourInputs

# Const jak k_intra — nie stroimy z settings.json.
SCENARIO_GRID_N = 5
QUANTILES: tuple[int, ...] = (10, 30, 50, 70, 90)
REPRESENTATIVE_NAME = "pv50_ld50"


@dataclass(frozen=True)
class PlanningScenario:
    """Jeden profil PV/load na cały horyzont."""

    name: str
    weight: float
    pv_kwh: tuple[float, ...]
    load_kwh: tuple[float, ...]


def scenario_name(pv_q: int, ld_q: int) -> str:
    return f"pv{int(pv_q)}_ld{int(ld_q)}"


def interpolate_from_knots(q: float, knots: list[tuple[float, float]]) -> float:
    """Interpolacja odcinkami z ekstrapolacją skrajnego odcinka; clamp energii ≥ 0."""
    pts = sorted(((float(k), float(v)) for k, v in knots), key=lambda p: p[0])
    if not pts:
        return 0.0
    if len(pts) == 1:
        return max(0.0, pts[0][1])
    qf = float(q)
    if qf <= pts[0][0]:
        q0, v0 = pts[0]
        q1, v1 = pts[1]
    elif qf >= pts[-1][0]:
        q0, v0 = pts[-2]
        q1, v1 = pts[-1]
    else:
        q0, v0 = pts[0]
        q1, v1 = pts[1]
        for i in range(len(pts) - 1):
            if pts[i][0] <= qf <= pts[i + 1][0]:
                q0, v0 = pts[i]
                q1, v1 = pts[i + 1]
                break
    if q1 == q0:
        return max(0.0, v0)
    t = (qf - q0) / (q1 - q0)
    return max(0.0, v0 + t * (v1 - v0))


def _pv_knots(hin: HourInputs) -> list[tuple[float, float]]:
    p50 = float(hin.pv_kwh)
    p10 = float(hin.pv_kwh_p10 if hin.pv_kwh_p10 is not None else p50)
    p90 = float(hin.pv_kwh_p90 if hin.pv_kwh_p90 is not None else p50)
    return [(10.0, p10), (50.0, p50), (90.0, p90)]


def _load_knots(hin: HourInputs) -> list[tuple[float, float]]:
    p50 = float(hin.load_kwh)
    p25 = float(hin.load_kwh_p25 if hin.load_kwh_p25 is not None else p50)
    p75 = float(hin.load_kwh_p75 if hin.load_kwh_p75 is not None else p50)
    return [(25.0, p25), (50.0, p50), (75.0, p75)]


def pv_at_quantile(hin: HourInputs, q: float) -> float:
    return interpolate_from_knots(q, _pv_knots(hin))


def load_at_quantile(hin: HourInputs, q: float) -> float:
    return interpolate_from_knots(q, _load_knots(hin))


def representative_scenario_index(scenarios: list[PlanningScenario]) -> int:
    for i, sc in enumerate(scenarios):
        if sc.name == REPRESENTATIVE_NAME:
            return i
    return 0


def collapse_nowcast_hour_indices(hours_in: list[HourInputs]) -> frozenset[int]:
    """Bieżący slot (indeks 0) i każda niepełna godzina: nowcast p50, nie wachlarz."""
    return frozenset(
        i
        for i, hin in enumerate(hours_in)
        if i == 0 or float(hin.hour_fraction) < 1.0 - 1e-9
    )


collapse_pv_hour_indices = collapse_nowcast_hour_indices


def build_planning_scenarios(
    hours_in: list[HourInputs],
    *,
    collapse_pv_hours: frozenset[int] | None = None,
    collapse_load_hours: frozenset[int] | None = None,
) -> list[PlanningScenario]:
    """25 niezależnych światów: PV q ∈ QUANTILES × load q ∈ QUANTILES, wagi 1/25.

    ``collapse_*_hours`` — te indeksy biorą ``hin.pv_kwh`` / ``hin.load_kwh``
    (nowcast) we wszystkich światach. Domyślnie pusto; optimizer podaje
    ``collapse_nowcast_hour_indices``.
    """
    if not hours_in:
        return []
    collapsed_pv = collapse_pv_hours if collapse_pv_hours is not None else frozenset()
    collapsed_ld = collapse_load_hours if collapse_load_hours is not None else frozenset()
    n = len(QUANTILES)
    weight = 1.0 / float(n * n)
    out: list[PlanningScenario] = []
    for q_pv in QUANTILES:
        for q_ld in QUANTILES:
            pv = tuple(
                float(hin.pv_kwh) if i in collapsed_pv else pv_at_quantile(hin, q_pv)
                for i, hin in enumerate(hours_in)
            )
            load = tuple(
                float(hin.load_kwh) if i in collapsed_ld else load_at_quantile(hin, q_ld)
                for i, hin in enumerate(hours_in)
            )
            out.append(
                PlanningScenario(
                    name=scenario_name(q_pv, q_ld),
                    weight=weight,
                    pv_kwh=pv,
                    load_kwh=load,
                )
            )
    return out
