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
