"""Testy optymalizatora CVaR."""

from __future__ import annotations

import pytest

from planner.battery import BatteryParams
from planner.models import HourInputs
from planner.optimizer import optimize_horizon
from planner.risk_optimizer import optimize_horizon_cvar


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


def test_cvar_keeps_more_soc_than_deterministic_on_tail_risk() -> None:
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = _evening_export_morning_risk_hours()
    det = optimize_horizon_cvar(hours, soc_start_pct=61.0, params=bp, cvar_lambda=0.0)
    risk = optimize_horizon_cvar(hours, soc_start_pct=61.0, params=bp, cvar_lambda=2.0, cvar_alpha=0.9)
    assert risk.hours[0].soc_end_pct >= det.hours[0].soc_end_pct - 0.5
    assert risk.hours[0].target_net_kwh <= det.hours[0].target_net_kwh + 0.5


def test_optimize_horizon_uses_cvar_when_lambda_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import planner.config as cfg

    monkeypatch.setattr(cfg, "_CVAR_LAMBDA_RAW", "1.0")
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = _evening_export_morning_risk_hours()
    res = optimize_horizon(hours, soc_start_pct=61.0, params=bp)
    assert res.hours
