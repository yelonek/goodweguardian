"""Testy eco-slot stochastic MILP."""

from __future__ import annotations

import pytest

from planner.battery import BatteryParams
from planner.models import HourInputs
from planner.optimizer import optimize_horizon
from planner.ecoslot_scenario_optimizer import optimize_horizon_ecoslot
from planner.policy_output import map_hour_to_exec_mode


def _pv_surplus_regression_hours() -> list[HourInputs]:
    return [
        HourInputs(
            date="2026-06-19",
            hour=14,
            load_kwh=2.2,
            pv_kwh=4.5,
            pv_kwh_p10=2.0,
            pv_kwh_p90=5.5,
            load_kwh_p75=8.5,
            load_kwh_p25=1.8,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.59,
        ),
        HourInputs(
            date="2026-06-19",
            hour=20,
            load_kwh=0.5,
            pv_kwh=0.1,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.2,
            load_kwh_p75=0.6,
            load_kwh_p25=0.4,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.78,
        ),
    ]


def test_ecoslot_no_charge_grid_on_pv_surplus() -> None:
    """Regresja 19.06: nadwyżka PV → neutral/charge_pv, nie charge_grid."""
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    res = optimize_horizon_ecoslot(_pv_surplus_regression_hours(), soc_start_pct=58.0, params=bp)
    h14 = res.hours[0]
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("model") == "ecoslot_shared_ch_dis"
    assert (h14.ch_grid_kwh or 0.0) <= 0.05
    row = map_hour_to_exec_mode(h14, _pv_surplus_regression_hours()[0])
    assert row.exec_mode != "charge_grid"


def test_ecoslot_exports_at_high_rce() -> None:
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-06-18",
            hour=18,
            load_kwh=0.8,
            pv_kwh=0.1,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.2,
            load_kwh_p75=1.0,
            load_kwh_p25=0.6,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.69,
        ),
    ]
    res = optimize_horizon_ecoslot(hours, soc_start_pct=70.0, params=bp)
    assert res.hours[0].target_net_kwh > 0.3
    assert res.hours[0].planned_exec_mode in ("export_profit", "export_pv_surplus")


def test_optimize_horizon_routes_ecoslot(monkeypatch: pytest.MonkeyPatch) -> None:
    import planner.config as cfg

    monkeypatch.setattr(cfg, "_OPTIMIZER_RAW", "ecoslot")
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    res = optimize_horizon(_pv_surplus_regression_hours(), soc_start_pct=58.0, params=bp)
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("model") == "ecoslot_shared_ch_dis"


def test_ecoslot_no_export_profit_without_net_export() -> None:
    """Rozładowanie przy net≈0 to neutral, nie export_profit."""
    hours = [
        HourInputs(
            date="2026-07-07",
            hour=6,
            load_kwh=0.46,
            pv_kwh=0.3,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.5,
            load_kwh_p75=0.7,
            load_kwh_p25=0.35,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.56,
        ),
        HourInputs(
            date="2026-07-07",
            hour=20,
            load_kwh=0.5,
            pv_kwh=0.1,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.2,
            load_kwh_p75=0.6,
            load_kwh_p25=0.4,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.78,
        ),
    ]
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    res = optimize_horizon_ecoslot(hours, soc_start_pct=19.0, params=bp)
    h6 = res.hours[0]
    if (h6.battery_delta_kwh or 0.0) < -0.05:
        assert abs(h6.target_net_kwh) <= 0.05
        assert h6.planned_exec_mode != "export_profit"


def test_ecoslot_no_charge_grid_on_expensive_import() -> None:
    """charge_grid tylko w taniej strefie importu."""
    hours = [
        HourInputs(
            date="2026-07-07",
            hour=13,
            load_kwh=1.44,
            pv_kwh=1.0,
            pv_kwh_p10=0.2,
            pv_kwh_p90=2.0,
            load_kwh_p75=2.8,
            load_kwh_p25=1.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.14,
        ),
        HourInputs(
            date="2026-07-07",
            hour=14,
            load_kwh=1.11,
            pv_kwh=2.0,
            pv_kwh_p10=0.5,
            pv_kwh_p90=3.0,
            load_kwh_p75=2.2,
            load_kwh_p25=0.8,
            import_pln_per_kwh=0.59,
            export_pln_per_kwh=0.14,
        ),
    ]
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    res = optimize_horizon_ecoslot(hours, soc_start_pct=10.0, params=bp)
    h13 = res.hours[0]
    assert (h13.ch_grid_kwh or 0.0) <= 0.05
    assert h13.planned_exec_mode != "charge_grid"


def test_ecoslot_shared_soc_across_scenarios() -> None:
    """SOC w planie = jedna trajektoria (nie per-scenariusz)."""
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    res = optimize_horizon_ecoslot(_pv_surplus_regression_hours(), soc_start_pct=58.0, params=bp)
    assert len(res.soc_trajectory_pct) == len(res.hours) + 1
    for hp in res.hours:
        assert 9.0 <= hp.soc_start_pct <= 100.5
        assert 9.0 <= hp.soc_end_pct <= 100.5
