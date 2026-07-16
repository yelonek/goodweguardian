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
        load_kwh=0.5,
        pv_kwh=0.0,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=1.154,
        hour_fraction=frac,
        net_so_far_kwh=3.13,
        load_so_far_kwh=0.4,
        pv_so_far_kwh=0.0,
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


def test_normalize_sets_remainder_from_net_so_far() -> None:
    hin, hp = _partial_hp(net=3.903, bd=-0.864)
    now = datetime(2026, 6, 19, 20, 50, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)
    assert out[0].target_net_kwh == pytest.approx(3.903)
    assert out[0].target_net_remainder_kwh == pytest.approx(0.773)
    assert out[0].battery_delta_kwh == pytest.approx(-0.864)


def test_normalize_discharge_pct_uses_remainder_battery_delta() -> None:
    hin, hp = _partial_hp(net=3.903, bd=-0.864)
    now = datetime(2026, 6, 19, 20, 50, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)[0]
    row = map_hour_to_exec_mode(out, hin)
    assert row.exec_mode == "export_profit"
    assert row.params.discharge_pct == 100


def test_mid_hour_pv_soak_not_charge_grid_after_prior_import() -> None:
    """MILP: net_end≈N₀ (intent≈0), PV→bateria → neutral, nie charge_grid."""
    frac = 40 / 60
    hin = HourInputs(
        date="2026-06-20",
        hour=12,
        load_kwh=1.43,
        pv_kwh=4.79,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=0.0,
        hour_fraction=frac,
        net_so_far_kwh=-0.5,
        load_so_far_kwh=0.5,
        pv_so_far_kwh=1.0,
    )
    hp = HourPlan(
        date="2026-06-20",
        hour=12,
        target_net_kwh=-0.5,
        expected_cashflow_pln=0.0,
        soc_start_pct=21.0,
        soc_end_pct=52.0,
        battery_delta_kwh=3.36 * frac,
    )
    now = datetime(2026, 6, 20, 12, 20, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)[0]
    assert out.target_net_kwh == pytest.approx(-0.5)
    assert out.target_net_remainder_kwh == pytest.approx(0.0)
    row = map_hour_to_exec_mode(out, hin)
    assert row.exec_mode == "neutral"
    assert row.params.allow_grid_charge is False


def test_normalize_zero_remainder_anchors_net_so_far_import() -> None:
    frac = 40 / 60
    hin = HourInputs(
        date="2026-07-08",
        hour=13,
        load_kwh=1.72,
        pv_kwh=3.09,
        import_pln_per_kwh=0.59,
        export_pln_per_kwh=0.0,
        hour_fraction=frac,
        net_so_far_kwh=-0.24,
    )
    hp = HourPlan(
        date="2026-07-08",
        hour=13,
        target_net_kwh=-0.24,
        expected_cashflow_pln=0.0,
        soc_start_pct=55.0,
        soc_end_pct=66.0,
        battery_delta_kwh=1.76 * frac,
    )
    now = datetime(2026, 7, 8, 13, 20, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)[0]
    assert out.target_net_kwh == pytest.approx(-0.24)
    assert out.target_net_remainder_kwh == pytest.approx(0.0)
    row = map_hour_to_exec_mode(out, hin)
    assert row.exec_mode == "neutral"


def test_mid_hour_full_target_with_prior_export_is_neutral_not_charge_grid() -> None:
    frac = 48 / 60
    hin = HourInputs(
        date="2026-07-08",
        hour=14,
        load_kwh=1.03,
        pv_kwh=3.10,
        import_pln_per_kwh=0.59,
        export_pln_per_kwh=0.0,
        hour_fraction=frac,
        net_so_far_kwh=0.31,
    )
    hp = HourPlan(
        date="2026-07-08",
        hour=14,
        target_net_kwh=0.31,
        target_net_remainder_kwh=0.0,
        expected_cashflow_pln=0.0,
        soc_start_pct=59.0,
        soc_end_pct=74.0,
        battery_delta_kwh=2.49,
    )
    row = map_hour_to_exec_mode(hp, hin)
    assert row.exec_mode == "neutral"
    assert row.params.allow_grid_charge is False


def test_normalize_zero_remainder_anchors_net_so_far_export() -> None:
    frac = 9 / 60
    hin = HourInputs(
        date="2026-07-08",
        hour=13,
        load_kwh=0.19,
        pv_kwh=0.46,
        import_pln_per_kwh=0.59,
        export_pln_per_kwh=0.0,
        hour_fraction=frac,
        net_so_far_kwh=0.05,
    )
    hp = HourPlan(
        date="2026-07-08",
        hour=13,
        target_net_kwh=0.05,
        expected_cashflow_pln=0.0,
        soc_start_pct=55.0,
        soc_end_pct=62.0,
        battery_delta_kwh=0.25,
    )
    now = datetime(2026, 7, 8, 13, 54, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)[0]
    assert out.target_net_kwh == pytest.approx(0.05)


def test_normalize_large_prior_import_remainder_zero_keeps_neutral() -> None:
    """Regresja 14:50: N₀=−5, intent=0 → target=−5, soak=neutral."""
    frac = 10 / 60
    hin = HourInputs(
        date="2026-07-16",
        hour=14,
        load_kwh=0.2,
        pv_kwh=0.7,
        import_pln_per_kwh=0.59,
        export_pln_per_kwh=0.38,
        hour_fraction=frac,
        net_so_far_kwh=-5.0,
    )
    hp = HourPlan(
        date="2026-07-16",
        hour=14,
        target_net_kwh=-5.0,
        expected_cashflow_pln=0.0,
        soc_start_pct=90.0,
        soc_end_pct=94.5,
        battery_delta_kwh=0.53,
    )
    now = datetime(2026, 7, 16, 14, 50, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)[0]
    assert out.target_net_kwh == pytest.approx(-5.0)
    assert out.target_net_remainder_kwh == pytest.approx(0.0)
    row = map_hour_to_exec_mode(out, hin)
    assert row.exec_mode == "neutral"
    assert row.params.allow_grid_charge is False
    assert row.params.target_net_kwh == pytest.approx(-5.0)


def test_mid_hour_discharge_serve_not_export_profit() -> None:
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
        net_so_far_kwh=0.0,
        load_so_far_kwh=0.0,
        pv_so_far_kwh=0.0,
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
    now = datetime(2026, 6, 20, 19, 40, 0)
    out = normalize_hour_plans_for_policy([hin], [hp], now=now)[0]
    assert out.battery_delta_kwh == pytest.approx(-0.099)
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
