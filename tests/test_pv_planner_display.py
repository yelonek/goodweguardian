"""Wejścia PV planera do dashboardu — spójność z MILP."""

from __future__ import annotations

from datetime import datetime

import pytest

from planner.pv_planner_display import (
    planner_pv_milp_snapshot,
    scaled_pv_bands_from_solcast,
)


def test_scaled_pv_bands_k_scale() -> None:
    row = {"pv_kw": 5.0, "pv_kw_p10": 2.0, "pv_kw_p90": 6.0}
    p50, p10, p90 = scaled_pv_bands_from_solcast(row, corrected_p50=5.5)
    assert p50 == pytest.approx(5.5)
    assert p10 == pytest.approx(2.2)
    assert p90 == pytest.approx(6.6)


def test_planner_pv_milp_snapshot_matches_hour_remainder() -> None:
    now = datetime(2026, 7, 16, 11, 40, 0)
    pv_row = {"pv_kw": 5.35, "pv_kw_p10": 2.39, "pv_kw_p90": 5.49}
    meta = {
        "a_so_far_kwh": 3.5,
        "recent_kw": 4.5,
        "alpha": 40 / 60,
        "k_intra": 1.2,
        "band_narrow_enabled": True,
    }
    snap = planner_pv_milp_snapshot(
        now=now,
        date_iso="2026-07-16",
        hour=11,
        pv_row=pv_row,
        pv_correction_meta=meta,
    )
    assert snap is not None
    assert snap["pv_planner_active"] is True
    assert snap["pv_planner_alpha"] == pytest.approx(40 / 60)
    assert snap["pv_planner_a_so_far_kwh"] == pytest.approx(3.5)
    assert (
        snap["pv_planner_remainder_p10_kwh"]
        <= snap["pv_planner_remainder_p50_kwh"]
        <= snap["pv_planner_remainder_p90_kwh"]
    )
    assert snap["pv_planner_remainder_p50_kwh"] < snap["pv_planner_full_p50_kwh"]


def test_planner_pv_milp_snapshot_none_for_other_hour() -> None:
    now = datetime(2026, 7, 16, 11, 40, 0)
    assert (
        planner_pv_milp_snapshot(
            now=now,
            date_iso="2026-07-16",
            hour=10,
            pv_row={"pv_kw": 1.0},
        )
        is None
    )
