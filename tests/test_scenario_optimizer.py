"""Testy optymalizatora wieloscenariuszowego."""

from __future__ import annotations

import pytest

from planner.battery import BatteryParams
from planner.models import HourInputs
from planner.optimizer import optimize_horizon
from planner.scenario_optimizer import optimize_horizon_scenarios


def _evening_export_morning_risk_hours() -> list[HourInputs]:
    """Wieczorny szczyt RCE + drogi poranek bez PV w pesymistycznym scenariuszu."""
    return [
        HourInputs(
            date="2026-06-14",
            hour=21,
            load_kwh=0.5,
            pv_kwh=0.02,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.05,
            load_kwh_p75=0.55,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.59,
        ),
        HourInputs(
            date="2026-06-14",
            hour=22,
            load_kwh=0.5,
            pv_kwh=0.0,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.0,
            load_kwh_p75=0.6,
            import_pln_per_kwh=0.59,
            export_pln_per_kwh=0.56,
        ),
        HourInputs(
            date="2026-06-15",
            hour=6,
            load_kwh=0.46,
            pv_kwh=0.3,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.5,
            load_kwh_p75=0.7,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.56,
        ),
        HourInputs(
            date="2026-06-15",
            hour=10,
            load_kwh=1.0,
            pv_kwh=1.5,
            pv_kwh_p10=0.2,
            pv_kwh_p90=2.0,
            load_kwh_p75=2.5,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.14,
        ),
        HourInputs(
            date="2026-06-15",
            hour=20,
            load_kwh=0.5,
            pv_kwh=0.2,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.4,
            load_kwh_p75=0.55,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.78,
        ),
    ]


def test_scenario_exports_at_high_rce() -> None:
    """Regresja: przy wysokim RCE planer musi eksportować, nie neutral."""
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
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.69,
        ),
        HourInputs(
            date="2026-06-19",
            hour=6,
            load_kwh=0.5,
            pv_kwh=0.1,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.3,
            load_kwh_p75=0.7,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.56,
        ),
    ]
    res = optimize_horizon_scenarios(hours, soc_start_pct=50.0, params=bp)
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("fallback") != "deterministic_p50"
    assert res.hours[0].target_net_kwh > 0.5


def test_optimize_horizon_uses_scenarios_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import planner.config as cfg

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "1")
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = _evening_export_morning_risk_hours()
    res = optimize_horizon(hours, soc_start_pct=61.0, params=bp)
    assert res.hours
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("model") == "per_scenario_ch_dis_soc"


def test_scenario_milp_no_grid_charge_when_pv_surplus() -> None:
    """Regresja 19.06 ~14:30: baza ładuje z PV, bez importu przy nadwyżce PV."""
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-06-19",
            hour=14,
            load_kwh=2.2,
            pv_kwh=4.5,
            pv_kwh_p10=2.0,
            pv_kwh_p90=5.5,
            load_kwh_p75=8.5,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.59,
        ),
        HourInputs(
            date="2026-06-19",
            hour=15,
            load_kwh=2.0,
            pv_kwh=3.8,
            pv_kwh_p10=1.5,
            pv_kwh_p90=4.5,
            load_kwh_p75=7.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.56,
        ),
        HourInputs(
            date="2026-06-19",
            hour=20,
            load_kwh=0.5,
            pv_kwh=0.1,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.2,
            load_kwh_p75=0.6,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.78,
        ),
    ]
    res = optimize_horizon_scenarios(hours, soc_start_pct=58.0, params=bp)
    h14 = res.hours[0]
    if h14.battery_delta_kwh > 0.05:
        assert h14.target_net_kwh >= -0.05
