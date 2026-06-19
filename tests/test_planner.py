"""Testy planera — optymalizator, audyt, economics."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from economics import cashflow_pln_for_hour
from planner.battery import BatteryParams, battery_delta_from_net
from planner.hour_remainder import hour_remaining_fraction, scale_hour_inputs_for_remainder
from planner.models import HourInputs
from planner.optimizer import optimize_horizon
from planner.audit import append_audit, new_event, read_audit_events
from planner.config import ensure_planner_dirs
import planner.audit as audit_mod
import planner.optimizer as opt_mod


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


def test_battery_wear_reduces_export_vs_no_wear(monkeypatch: pytest.MonkeyPatch) -> None:
    import planner.optimizer as opt_mod

    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-06-09",
            hour=15,
            load_kwh=0.5,
            pv_kwh=3.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.35,
        ),
    ]
    monkeypatch.setattr(opt_mod, "PLANNER_BATTERY_CYCLE_COST_PLN", 0.0)
    no_wear = optimize_horizon(hours, soc_start_pct=80.0, params=bp)
    monkeypatch.setattr(opt_mod, "PLANNER_BATTERY_CYCLE_COST_PLN", 0.10)
    with_wear = optimize_horizon(hours, soc_start_pct=80.0, params=bp)
    assert with_wear.hours[0].target_net_kwh <= no_wear.hours[0].target_net_kwh + 1e-6
    if with_wear.hours[0].battery_wear_cost_pln > 0:
        grid = cashflow_pln_for_hour(
            with_wear.hours[0].target_net_kwh,
            rce_pln_per_kwh=0.35,
            import_pln_per_kwh=1.11,
        )
        assert with_wear.hours[0].expected_cashflow_pln == pytest.approx(
            grid - with_wear.hours[0].battery_wear_cost_pln
        )


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
    hp = res.hours[0]
    assert hp.target_net_kwh >= 0.0
    grid_cf = cashflow_pln_for_hour(
        hp.target_net_kwh,
        rce_pln_per_kwh=2.0,
        import_pln_per_kwh=0.8,
    )
    assert hp.expected_cashflow_pln == pytest.approx(grid_cf - hp.battery_wear_cost_pln)
    assert res.total_cashflow_pln > 0.0


def test_audit_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_mod, "PLANNER_AUDIT_DIR", tmp_path)
    ensure_planner_dirs()
    ev = new_event(local_date="2026-05-01", kind="plan_created", plan_id="p1", payload={"x": 1})
    append_audit(ev)
    got = read_audit_events("2026-05-01")
    assert len(got) == 1
    assert got[0].kind == "plan_created"
    assert got[0].payload["x"] == 1


def test_hour_remaining_fraction_at_fifty_minutes() -> None:
    now = datetime(2026, 6, 14, 20, 50, 0)
    frac = hour_remaining_fraction(now, date="2026-06-14", hour=20)
    assert frac == pytest.approx(10 / 60, rel=0.01)
    assert hour_remaining_fraction(now, date="2026-06-14", hour=21) == 1.0


def test_scale_hour_inputs_for_remainder() -> None:
    now = datetime(2026, 6, 14, 20, 50, 0)
    hin = HourInputs(
        date="2026-06-14",
        hour=20,
        load_kwh=0.6,
        pv_kwh=0.12,
        pv_kwh_p10=0.05,
        pv_kwh_p90=0.2,
        load_kwh_p75=0.7,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=1.69,
    )
    scaled = scale_hour_inputs_for_remainder(
        hin,
        now=now,
        pv_correction_meta={"a_so_far_kwh": 0.02},
    )
    assert scaled.hour_fraction == pytest.approx(10 / 60, rel=0.01)
    assert scaled.load_kwh == pytest.approx(0.6 * 10 / 60)
    assert scaled.load_kwh_p75 == pytest.approx(0.7 * 10 / 60)
    assert scaled.pv_kwh == pytest.approx(0.10)
    assert scaled.pv_kwh_p10 == pytest.approx(0.03)
    assert scaled.pv_kwh_p90 == pytest.approx(0.18)


def test_partial_current_hour_limits_soc_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regresja 20:50: nie planuj końca h20 na ~10% SOC przy starcie ~59%."""
    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    frac = 10 / 60
    hours = [
        HourInputs(
            date="2026-06-14",
            hour=20,
            load_kwh=0.5 * frac,
            pv_kwh=0.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.69,
            hour_fraction=frac,
        ),
        HourInputs(
            date="2026-06-14",
            hour=21,
            load_kwh=0.5,
            pv_kwh=0.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.69,
        ),
    ]
    res = optimize_horizon(hours, soc_start_pct=59.0, params=bp)
    # max ~0.83 kWh discharge w reszcie h20 → SOC nie spada o ~50 pp jak przy pełnej h
    assert res.hours[0].soc_end_pct > 45.0
    assert res.hours[1].target_net_kwh > 0.3


def test_partial_hour_keeps_more_soc_than_full_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    base = dict(
        date="2026-06-14",
        hour=20,
        load_kwh=0.5,
        pv_kwh=0.0,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=1.69,
    )
    full = optimize_horizon(
        [HourInputs(**base, hour_fraction=1.0)],
        soc_start_pct=59.0,
        params=bp,
    )
    partial = optimize_horizon(
        [
            HourInputs(
                date=base["date"],
                hour=base["hour"],
                load_kwh=0.5 * (10 / 60),
                pv_kwh=base["pv_kwh"],
                import_pln_per_kwh=base["import_pln_per_kwh"],
                export_pln_per_kwh=base["export_pln_per_kwh"],
                hour_fraction=10 / 60,
            )
        ],
        soc_start_pct=59.0,
        params=bp,
    )
    assert partial.hours[0].soc_end_pct > full.hours[0].soc_end_pct
