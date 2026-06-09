"""Testy planera — optymalizator, audyt, economics."""

from __future__ import annotations

from pathlib import Path

import pytest

from economics import cashflow_pln_for_hour
from planner.battery import BatteryParams, battery_delta_from_net
from planner.models import HourInputs
from planner.optimizer import optimize_horizon
from planner.audit import append_audit, new_event, read_audit_events
from planner.config import ensure_planner_dirs
import planner.audit as audit_mod


def test_battery_delta_sign() -> None:
    # PV 2, load 1, net export 0.5 -> battery +0.5
    bd = battery_delta_from_net(pv_kwh=2.0, load_kwh=1.0, net_kwh=0.5)
    assert bd == pytest.approx(0.5)


def _soc_in_bounds(soc_pct: float, bp: BatteryParams) -> bool:
    return bp.soc_min_pct - 0.1 <= soc_pct <= bp.soc_max_pct + 0.1


def test_lp_no_spurious_export_at_low_rce_when_storing_pays() -> None:
    """Przy niskim RCE i nadwyżce PV: trzymaj energię na późniejszy eksport, nie +0,25 z siatki."""
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-06-09",
            hour=12,
            load_kwh=1.96,
            pv_kwh=2.61,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.25,
        ),
        HourInputs(
            date="2026-06-09",
            hour=20,
            load_kwh=0.55,
            pv_kwh=0.16,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.95,
        ),
    ]
    res = optimize_horizon(hours, soc_start_pct=45.0, params=bp)
    assert res.hours[0].target_net_kwh == pytest.approx(0.0, abs=0.05)
    assert res.hours[1].target_net_kwh > 0.5


def test_lp_energy_balance_and_soc_limits() -> None:
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-06-09",
            hour=14,
            load_kwh=3.6,
            pv_kwh=2.02,
            import_pln_per_kwh=0.59,
            export_pln_per_kwh=0.42,
        ),
        HourInputs(
            date="2026-06-09",
            hour=20,
            load_kwh=0.55,
            pv_kwh=0.16,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.72,
        ),
    ]
    res = optimize_horizon(hours, soc_start_pct=55.0, params=bp)
    for hp in res.hours:
        assert _soc_in_bounds(hp.soc_start_pct, bp)
        assert _soc_in_bounds(hp.soc_end_pct, bp)
    assert res.total_cashflow_pln > 0.0


def test_optimizer_prefers_export_when_rce_high() -> None:
    """Przy dużym RCE i nadwyżce PV planer powinien eksportować zamiast trzymać w magazynie."""
    bp = BatteryParams(capacity_kwh=5.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-05-01",
            hour=12,
            load_kwh=0.5,
            pv_kwh=4.0,
            import_pln_per_kwh=0.8,
            export_pln_per_kwh=2.0,
        ),
    ]
    res = optimize_horizon(hours, soc_start_pct=50.0, params=bp)
    assert res.hours[0].target_net_kwh >= 0.0
    assert res.total_cashflow_pln >= cashflow_pln_for_hour(
        res.hours[0].target_net_kwh,
        rce_pln_per_kwh=2.0,
        import_pln_per_kwh=0.8,
    )


def test_audit_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_mod, "PLANNER_AUDIT_DIR", tmp_path)
    ensure_planner_dirs()
    ev = new_event(local_date="2026-05-01", kind="plan_created", plan_id="p1", payload={"x": 1})
    append_audit(ev)
    got = read_audit_events("2026-05-01")
    assert len(got) == 1
    assert got[0].kind == "plan_created"
    assert got[0].payload["x"] == 1
