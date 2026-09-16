"""Siatka 5×5 kwantyli PV×load."""

from __future__ import annotations

import pytest

from planner.models import HourInputs
from planner.scenarios import (
    QUANTILES,
    REPRESENTATIVE_NAME,
    SCENARIO_GRID_N,
    build_planning_scenarios,
    interpolate_from_knots,
    load_at_quantile,
    pv_at_quantile,
    representative_scenario_index,
    scenario_name,
)


def test_quantile_knots_and_lerp() -> None:
    knots = [(10.0, 1.0), (50.0, 5.0), (90.0, 9.0)]
    assert interpolate_from_knots(10, knots) == pytest.approx(1.0)
    assert interpolate_from_knots(30, knots) == pytest.approx(3.0)
    assert interpolate_from_knots(50, knots) == pytest.approx(5.0)
    assert interpolate_from_knots(70, knots) == pytest.approx(7.0)
    assert interpolate_from_knots(90, knots) == pytest.approx(9.0)


def test_load_quantile_extrapolates_then_clamps() -> None:
    hin = HourInputs(
        date="2026-09-15",
        hour=12,
        load_kwh=2.0,
        load_kwh_p25=1.0,
        load_kwh_p75=3.0,
        pv_kwh=4.0,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=0.4,
    )
    assert load_at_quantile(hin, 10) == pytest.approx(0.4)
    assert load_at_quantile(hin, 90) == pytest.approx(3.6)
    neg = HourInputs(
        date="2026-09-15",
        hour=12,
        load_kwh=0.2,
        load_kwh_p25=0.1,
        load_kwh_p75=0.3,
        pv_kwh=0.0,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=0.4,
    )
    assert load_at_quantile(neg, 10) >= 0.0


def test_grid_25_equal_weights_and_monotonic_cell() -> None:
    hin = HourInputs(
        date="2026-09-15",
        hour=12,
        load_kwh=2.0,
        load_kwh_p25=1.0,
        load_kwh_p75=4.0,
        pv_kwh=5.0,
        pv_kwh_p10=1.0,
        pv_kwh_p90=9.0,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=0.4,
    )
    scs = build_planning_scenarios([hin])
    assert SCENARIO_GRID_N == 5
    assert QUANTILES == (10, 30, 50, 70, 90)
    assert len(scs) == 25
    assert sum(s.weight for s in scs) == pytest.approx(1.0)
    assert {s.name for s in scs} == {
        scenario_name(qpv, qld) for qpv in QUANTILES for qld in QUANTILES
    }
    idx = representative_scenario_index(scs)
    assert scs[idx].name == REPRESENTATIVE_NAME
    pv_at_ld50 = [s.pv_kwh[0] for s in scs if s.name.endswith("_ld50")]
    assert pv_at_ld50 == sorted(pv_at_ld50)
    ld_at_pv50 = [s.load_kwh[0] for s in scs if s.name.startswith("pv50_")]
    assert ld_at_pv50 == sorted(ld_at_pv50)
    cell = next(s for s in scs if s.name == "pv10_ld90")
    assert cell.pv_kwh[0] == pytest.approx(pv_at_quantile(hin, 10))
    assert cell.load_kwh[0] == pytest.approx(load_at_quantile(hin, 90))
    assert cell.pv_kwh[0] < next(s for s in scs if s.name == "pv50_ld90").pv_kwh[0]
    assert cell.load_kwh[0] > next(s for s in scs if s.name == "pv10_ld50").load_kwh[0]
