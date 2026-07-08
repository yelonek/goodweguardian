"""Testy pełnogodzinnej semantyki HourPlan po rolling replan."""

from __future__ import annotations

from datetime import datetime

import pytest

from planner.hour_plan_export import normalize_hour_plans_for_policy
from planner.models import HourInputs, HourPlan
from planner.policy_output import map_hour_to_exec_mode


def _partial_hp(*, net: float, bd: float) -> tuple[HourInputs, HourPlan]:
    frac = 10 / 60
    hin = HourInputs(
        date="2026-06-19",
        hour=20,
        load_kwh=0.5 * frac,
        pv_kwh=0.0,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=1.154,
        hour_fraction=frac,
    )
    hp = HourPlan(
        date="2026-06-19",
        hour=20,
        target_net_kwh=net,
        expected_cashflow_pln=0.8,
        soc_start_pct=55.0,
        soc_end_pct=45.6,
        battery_delta_kwh=bd,
    )
    return hin, hp


def test_normalize_extrapolates_without_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "planner.hour_plan_export.net_kwh_so_far_for_hour",
        lambda _d, _h: None,
    )
    hin, hp = _partial_hp(net=0.773, bd=-0.864)
    now = datetime(2026, 6, 19, 20, 50, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)
    assert out[0].target_net_kwh == pytest.approx(0.773 / (10 / 60), rel=0.01)
    assert out[0].battery_delta_kwh == pytest.approx(-0.864 / (10 / 60), rel=0.01)


def test_normalize_adds_actual_net_with_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "planner.hour_plan_export.net_kwh_so_far_for_hour",
        lambda _d, _h: 3.13,
    )
    hin, hp = _partial_hp(net=0.773, bd=-0.864)
    now = datetime(2026, 6, 19, 20, 50, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)
    assert out[0].target_net_kwh == pytest.approx(3.903, rel=0.01)


def test_normalize_discharge_pct_uses_full_hour_battery_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "planner.hour_plan_export.net_kwh_so_far_for_hour",
        lambda _d, _h: 3.13,
    )
    monkeypatch.setattr(
        "planner.policy_output.net_kwh_so_far_for_hour",
        lambda _d, _h: 3.13,
    )
    hin, hp = _partial_hp(net=0.773, bd=-0.864)
    now = datetime(2026, 6, 19, 20, 50, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)[0]
    row = map_hour_to_exec_mode(out, hin)
    assert row.exec_mode == "export_profit"
    assert row.params.discharge_pct == 100


def test_mid_hour_pv_soak_not_charge_grid_after_prior_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MILP: net=0 na resztę h (PV→bateria); wcześniejszy import ≠ charge_grid."""
    frac = 40 / 60
    hin = HourInputs(
        date="2026-06-20",
        hour=12,
        load_kwh=1.43 * frac,
        pv_kwh=4.79 * frac,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=0.0,
        hour_fraction=frac,
    )
    hp = HourPlan(
        date="2026-06-20",
        hour=12,
        target_net_kwh=0.0,
        expected_cashflow_pln=0.0,
        soc_start_pct=21.0,
        soc_end_pct=52.0,
        battery_delta_kwh=3.36,
    )
    monkeypatch.setattr(
        "planner.hour_plan_export.net_kwh_so_far_for_hour",
        lambda _d, _h: -0.5,
    )
    monkeypatch.setattr(
        "planner.policy_output.net_kwh_so_far_for_hour",
        lambda _d, _h: -0.5,
    )
    now = datetime(2026, 6, 20, 12, 20, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)[0]
    # remainder=0 → cel 0, nie kotwica net_so_far z telemetrii.
    assert out.target_net_kwh == pytest.approx(0.0)
    assert out.battery_delta_kwh == pytest.approx(3.36 / frac, rel=0.01)
    row = map_hour_to_exec_mode(out, hin)
    assert row.exec_mode == "neutral"
    assert row.params.allow_grid_charge is False


def test_normalize_zero_remainder_ignores_net_so_far_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remainder=0 + ujemny net_so_far → cel 0, nie kotwica."""
    frac = 40 / 60
    hin = HourInputs(
        date="2026-07-08",
        hour=13,
        load_kwh=1.72 * frac,
        pv_kwh=3.09 * frac,
        import_pln_per_kwh=0.59,
        export_pln_per_kwh=0.0,
        hour_fraction=frac,
    )
    hp = HourPlan(
        date="2026-07-08",
        hour=13,
        target_net_kwh=0.0,
        expected_cashflow_pln=0.0,
        soc_start_pct=55.0,
        soc_end_pct=66.0,
        battery_delta_kwh=1.76,
    )
    monkeypatch.setattr(
        "planner.hour_plan_export.net_kwh_so_far_for_hour",
        lambda _d, _h: -0.24,
    )
    monkeypatch.setattr(
        "planner.policy_output.net_kwh_so_far_for_hour",
        lambda _d, _h: -0.24,
    )
    now = datetime(2026, 7, 8, 13, 20, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)[0]
    assert out.target_net_kwh == pytest.approx(0.0)
    row = map_hour_to_exec_mode(out, hin)
    assert row.exec_mode == "neutral"


def test_normalize_zero_remainder_ignores_net_so_far_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remainder=0 + dodatni net_so_far (RCE=0) → cel 0, nie kotwica +0,05."""
    frac = 9 / 60
    hin = HourInputs(
        date="2026-07-08",
        hour=13,
        load_kwh=0.19 * frac,
        pv_kwh=0.46 * frac,
        import_pln_per_kwh=0.59,
        export_pln_per_kwh=0.0,
        hour_fraction=frac,
    )
    hp = HourPlan(
        date="2026-07-08",
        hour=13,
        target_net_kwh=0.0,
        expected_cashflow_pln=0.0,
        soc_start_pct=55.0,
        soc_end_pct=62.0,
        battery_delta_kwh=0.25,
    )
    monkeypatch.setattr(
        "planner.hour_plan_export.net_kwh_so_far_for_hour",
        lambda _d, _h: 0.05,
    )
    now = datetime(2026, 7, 8, 13, 54, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)[0]
    assert out.target_net_kwh == pytest.approx(0.0)


def test_mid_hour_discharge_serve_not_export_profit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MILP: bd≈−serve, net=0 na resztę h → neutral, nie export_profit z ekstrapolacji bd."""
    frac = 20 / 60
    serve = 0.099
    hin = HourInputs(
        date="2026-06-20",
        hour=19,
        load_kwh=0.15,
        pv_kwh=0.15 - serve,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=0.884,
        hour_fraction=frac,
    )
    hp = HourPlan(
        date="2026-06-20",
        hour=19,
        target_net_kwh=0.0,
        expected_cashflow_pln=0.0,
        soc_start_pct=72.0,
        soc_end_pct=71.0,
        battery_delta_kwh=-0.099,
    )
    monkeypatch.setattr(
        "planner.hour_plan_export.net_kwh_so_far_for_hour",
        lambda _d, _h: None,
    )
    now = datetime(2026, 6, 20, 19, 40, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)[0]
    assert out.battery_delta_kwh == pytest.approx(-0.099 / frac, rel=0.01)
    row = map_hour_to_exec_mode(out, hin)
    assert row.exec_mode == "neutral"
    assert row.params.discharge_pct is None


def test_full_hour_slot_unchanged() -> None:
    hin = HourInputs(
        date="2026-06-19",
        hour=21,
        load_kwh=0.5,
        pv_kwh=0.0,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=1.027,
        hour_fraction=1.0,
    )
    hp = HourPlan(
        date="2026-06-19",
        hour=21,
        target_net_kwh=2.71,
        expected_cashflow_pln=2.5,
        soc_start_pct=45.0,
        soc_end_pct=10.0,
        battery_delta_kwh=-3.28,
    )
    now = datetime(2026, 6, 19, 20, 50, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)
    assert out[0].target_net_kwh == pytest.approx(2.71)
    assert out[0].battery_delta_kwh == pytest.approx(-3.28)
