"""Nocny anty-flipflop: MILP + mapper nie ładują z sieci i nie eksportują w tym samym oknie 22–5."""

from __future__ import annotations

from datetime import datetime

import pytest

from planner.battery import BatteryParams
from planner.models import DailyPlan, HourInputs, HourPlan
from planner.night_grid_policy import (
    NIGHT_GRID_HOURS,
    completed_night_slots_before,
    hour_was_night_grid_charge,
    night_export_blocked_from_plans,
    night_grid_charge_carry_in,
    night_windows,
)
from planner.optimizer import optimize_horizon
from planner.policy_output import build_policy_artifact, map_hour_to_exec_mode

BP = BatteryParams(
    capacity_kwh=10.77,
    soc_min_pct=11.0,
    soc_max_pct=100.0,
    max_power_kwh_per_h=5.0,
)


def _hin(
    hour: int,
    *,
    date: str = "2026-08-27",
    load: float = 0.3,
    pv: float = 0.0,
    imp: float = 0.59,
    exp: float = 0.1,
) -> HourInputs:
    return HourInputs(
        date=date,
        hour=hour,
        load_kwh=load,
        pv_kwh=pv,
        import_pln_per_kwh=imp,
        export_pln_per_kwh=exp,
    )


def test_night_windows_midnight_is_one_window() -> None:
    hours = [
        _hin(22, date="2026-08-26"),
        _hin(23, date="2026-08-26"),
        _hin(0),
        _hin(1),
        _hin(5),
        _hin(6),
        _hin(13),
        _hin(22, date="2026-08-27"),
    ]
    windows = night_windows(hours)
    assert windows[0] == [0, 1, 2, 3, 4]
    assert windows[1] == [7]
    assert 13 not in NIGHT_GRID_HOURS


def test_completed_night_slots_before_wraps_midnight() -> None:
    now = datetime(2026, 8, 27, 3, 10, 0)
    slots = completed_night_slots_before(now)
    assert slots == [
        ("2026-08-26", 22),
        ("2026-08-26", 23),
        ("2026-08-27", 0),
        ("2026-08-27", 1),
        ("2026-08-27", 2),
    ]
    assert completed_night_slots_before(datetime(2026, 8, 27, 12, 0, 0)) == []
    assert completed_night_slots_before(datetime(2026, 8, 26, 22, 5, 0)) == []
    assert completed_night_slots_before(datetime(2026, 8, 26, 23, 5, 0)) == [
        ("2026-08-26", 22)
    ]


def test_hour_was_night_grid_charge_soc_rise_without_pv() -> None:
    rows = [
        {"local_hour": 1, "local_minute": 0, "soc_pct": 20.0, "pv_w": 0.0},
        {"local_hour": 1, "local_minute": 59, "soc_pct": 48.0, "pv_w": 0.0},
    ]
    assert hour_was_night_grid_charge(rows, 1) is True


def test_hour_was_night_grid_charge_ignores_pv_soak() -> None:
    rows = [
        {"local_hour": 22, "local_minute": 0, "soc_pct": 70.0, "pv_w": 800.0},
        {"local_hour": 22, "local_minute": 59, "soc_pct": 78.0, "pv_w": 800.0},
    ]
    assert hour_was_night_grid_charge(rows, 22) is False


def test_night_grid_charge_carry_in_from_completed_hour() -> None:
    now = datetime(2026, 8, 27, 3, 0, 0)
    by_date = {
        "2026-08-26": [],
        "2026-08-27": [
            {"local_hour": 1, "local_minute": 0, "soc_pct": 22.0, "pv_w": 0.0},
            {"local_hour": 1, "local_minute": 50, "soc_pct": 51.0, "pv_w": 0.0},
        ],
    }
    assert night_grid_charge_carry_in(now, telemetry_rows_by_date=by_date) is True
    by_date["2026-08-27"][1]["soc_pct"] = 22.4
    assert night_grid_charge_carry_in(now, telemetry_rows_by_date=by_date) is False


def test_milp_no_charge_then_export_same_night(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tani import o 1:00, drogi RCE o 2:00 — nie ładuj i nie sprzedawaj w tej samej nocy."""
    import planner.optimizer as opt_mod

    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    hours = [
        _hin(1, imp=0.20, exp=0.05),
        _hin(2, imp=0.20, exp=2.00),
    ]
    res = optimize_horizon(hours, soc_start_pct=30.0, params=BP)
    # Spread 0,20 → 2,00 PLN/kWh kusi zakup, ale zapadka go blokuje.
    assert res.hours[0].battery_delta_kwh < 0.2
    assert res.hours[1].target_net_kwh > 0.5


def test_milp_idle_hour_does_not_bypass_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    """ch@1, jałowa@2, exp@3 — nadal zakazane (zapadka, nie tylko sąsiednie godziny)."""
    import planner.optimizer as opt_mod

    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    hours = [
        _hin(1, imp=0.20, exp=0.05),
        _hin(2, imp=0.20, exp=0.05),
        _hin(3, imp=0.20, exp=2.00),
    ]
    res = optimize_horizon(hours, soc_start_pct=30.0, params=BP)
    assert all(h.battery_delta_kwh < 0.2 for h in res.hours[:2])
    assert res.hours[2].target_net_kwh > 0.5


def test_milp_evening_export_then_night_charge_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SOC 80%, wysokie RCE o 22, tani import o 0 — wolno eksport 22, potem charge 0."""
    import planner.optimizer as opt_mod

    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    hours = [
        _hin(22, date="2026-08-26", imp=1.11, exp=1.50),
        _hin(23, date="2026-08-26", imp=0.59, exp=0.10),
        _hin(0, imp=0.59, exp=0.10),
        _hin(7, imp=1.11, exp=1.60),
    ]
    res = optimize_horizon(hours, soc_start_pct=80.0, params=BP)
    assert res.hours[0].target_net_kwh > 0.5
    charged_after = (
        res.hours[2].battery_delta_kwh > 0.3 or res.hours[2].target_net_kwh < -0.3
    )
    assert charged_after


def test_milp_charge_at_5_export_at_7_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Charge o 5 (noc), export_profit o 7 (poza oknem) zostaje."""
    import planner.optimizer as opt_mod

    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    hours = [
        _hin(5, imp=0.59, exp=0.10),
        _hin(6, imp=1.11, exp=0.30),
        _hin(7, imp=1.11, exp=1.80),
    ]
    res = optimize_horizon(hours, soc_start_pct=20.0, params=BP)
    assert res.hours[0].battery_delta_kwh > 0.5
    assert res.hours[2].target_net_kwh > 1.0


def test_milp_carry_in_blocks_remaining_night_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import planner.optimizer as opt_mod

    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    hours = [
        _hin(3, imp=0.59, exp=2.00),
        _hin(4, imp=0.59, exp=2.00),
        _hin(5, imp=0.59, exp=2.00),
    ]
    res = optimize_horizon(
        hours, soc_start_pct=80.0, params=BP, night_charge_carry_in=True
    )
    assert all(h.target_net_kwh <= 0.05 for h in res.hours)


def test_milp_midday_g12_charge_evening_export_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Okno G12 13–14 nie jest nocą — arbitraż 13→19 zostaje."""
    import planner.optimizer as opt_mod

    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    hours = [
        _hin(13, load=0.5, pv=1.0, imp=0.59, exp=0.20),
        _hin(14, load=0.5, pv=1.0, imp=0.59, exp=0.20),
        _hin(19, load=0.5, pv=0.1, imp=1.11, exp=1.50),
    ]
    res = optimize_horizon(hours, soc_start_pct=20.0, params=BP)
    bought = max(0.0, -res.hours[0].target_net_kwh) + max(
        0.0, -res.hours[1].target_net_kwh
    )
    assert bought > 0.5 or res.hours[0].battery_delta_kwh > 0.3
    assert res.hours[2].target_net_kwh > 1.0


def test_tracking_milp_also_blocks_night_flipflop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tracking-SP (scenariusze włączone) też nie kupuje w nocy pod wieczorny zrzut w tym samym oknie."""
    import planner.config as cfg
    import planner.optimizer as opt_mod

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "1")
    monkeypatch.setattr(cfg, "_SOC_TRACKING_RAW", "1")
    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: True)
    hours = [
        _hin(1, imp=0.20, exp=0.05),
        _hin(2, imp=0.20, exp=2.00),
    ]
    res = optimize_horizon(hours, soc_start_pct=30.0, params=BP)
    assert res.hours[0].battery_delta_kwh < 0.2
    assert res.hours[1].target_net_kwh > 0.5
    row = map_hour_to_exec_mode(
        HourPlan(
            date="2026-08-27",
            hour=4,
            target_net_kwh=4.0,
            expected_cashflow_pln=6.0,
            soc_start_pct=80.0,
            soc_end_pct=20.0,
            battery_delta_kwh=-5.0,
        ),
        HourInputs(
            date="2026-08-27",
            hour=4,
            load_kwh=0.3,
            pv_kwh=0.0,
            import_pln_per_kwh=0.59,
            export_pln_per_kwh=1.5,
        ),
        night_export_blocked=True,
    )
    assert row.exec_mode == "neutral"
    assert row.params.discharge_pct is None


def test_blocked_slots_after_planned_night_charge() -> None:
    plans = [
        HourPlan(
            date="2026-08-27",
            hour=1,
            target_net_kwh=-3.0,
            expected_cashflow_pln=-1.8,
            soc_start_pct=20.0,
            soc_end_pct=50.0,
            battery_delta_kwh=3.0,
        ),
        HourPlan(
            date="2026-08-27",
            hour=2,
            target_net_kwh=4.0,
            expected_cashflow_pln=8.0,
            soc_start_pct=50.0,
            soc_end_pct=15.0,
            battery_delta_kwh=-3.5,
        ),
    ]
    blocked = night_export_blocked_from_plans(plans)
    assert ("2026-08-27", 1) in blocked
    assert ("2026-08-27", 2) in blocked


def test_policy_artifact_remaps_night_export_after_charge() -> None:
    hours_in = [_hin(1, imp=0.59, exp=0.1), _hin(2, imp=0.59, exp=1.5)]
    plans = [
        HourPlan(
            date="2026-08-27",
            hour=1,
            target_net_kwh=-3.0,
            expected_cashflow_pln=-1.8,
            soc_start_pct=20.0,
            soc_end_pct=50.0,
            battery_delta_kwh=3.0,
        ),
        HourPlan(
            date="2026-08-27",
            hour=2,
            target_net_kwh=4.0,
            expected_cashflow_pln=6.0,
            soc_start_pct=50.0,
            soc_end_pct=15.0,
            battery_delta_kwh=-3.5,
        ),
    ]
    plan = DailyPlan(
        plan_id="test-night-latch",
        local_date="2026-08-27",
        generated_at="2026-08-27T01:00:00+00:00",
        timezone="Europe/Warsaw",
        soc_start_pct=20.0,
        expected_total_cashflow_pln=0.0,
        optimizer="test",
        inputs_snapshot={},
        hours=plans,
    )
    art = build_policy_artifact(plan, hours_in)
    assert art.hours[0].exec_mode == "charge_grid"
    assert art.hours[1].exec_mode == "neutral"
