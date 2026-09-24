"""Testy modułu economics — spójność z logiką KPI (depozyt − rachunek)."""

from __future__ import annotations

import pytest

from economics import (
    avg_export_pln_per_kwh,
    battery_wear_pln_for_hour,
    cashflow_pln_for_hour,
    settle_seller_hour,
    total_cashflow_pln_for_horizon,
)


def test_export_positive() -> None:
    assert cashflow_pln_for_hour(
        2.0, rce_pln_per_kwh=0.5, import_pln_per_kwh=0.9
    ) == pytest.approx(1.0)


def test_import_negative() -> None:
    assert cashflow_pln_for_hour(
        -3.0, rce_pln_per_kwh=0.5, import_pln_per_kwh=0.8
    ) == pytest.approx(-2.4)


def test_zero() -> None:
    assert (
        cashflow_pln_for_hour(0.0, rce_pln_per_kwh=1.0, import_pln_per_kwh=1.0) == 0.0
    )


def test_negative_rce_export_floor_zero() -> None:
    """Prosument: ujemna RCE nie daje ujemnego przychodu z eksportu."""
    assert cashflow_pln_for_hour(
        2.0, rce_pln_per_kwh=-0.15, import_pln_per_kwh=1.0
    ) == pytest.approx(0.0)


def test_export_pln_per_kwh_effective() -> None:
    from economics import export_pln_per_kwh_effective

    assert export_pln_per_kwh_effective(0.42) == pytest.approx(0.42)
    assert export_pln_per_kwh_effective(-0.1) == pytest.approx(0.0)


def test_avg_export_pln_per_kwh() -> None:
    assert avg_export_pln_per_kwh(10.0, 25.0) == pytest.approx(0.4)
    assert avg_export_pln_per_kwh(0.0, 5.0) == pytest.approx(0.0)
    assert avg_export_pln_per_kwh(1.0, 0.0) is None
    assert avg_export_pln_per_kwh(1.0, -0.01) is None


def test_matches_kpi_style_deposit_minus_bill() -> None:
    """Jak w ``_kpi_for_day``: depozyt += surplus*rce, bill += deficit*eff."""
    rce, imp = 0.4, 0.7
    net_export = 5.0
    net_import = -2.0
    deposit = net_export * rce
    bill = (-net_import) * imp
    kpi_net = deposit - bill
    cf = total_cashflow_pln_for_horizon(
        [
            (net_export, rce, imp),
            (net_import, rce, imp),
        ]
    )
    assert cf == pytest.approx(kpi_net)


def test_battery_wear_on_discharge_only() -> None:
    assert battery_wear_pln_for_hour(1.0, 1.0, cycle_cost_pln=0.10) == pytest.approx(0.10)
    assert battery_wear_pln_for_hour(1.0, 0.0, cycle_cost_pln=0.10) == pytest.approx(0.0)
    assert battery_wear_pln_for_hour(0.0, 1.0, cycle_cost_pln=0.10) == pytest.approx(0.10)
    assert battery_wear_pln_for_hour(1.0, 1.0, cycle_cost_pln=0.0) == 0.0


def test_open_hour_positive_meter_above_plan_costs_unsold_rce() -> None:
    """16 września, 12:xx: +0,97 na liczniku, plan +0,23, RCE 0,48."""
    settled = settle_seller_hour(
        0.23,
        current_balance_kwh=0.97,
        rce_pln_per_kwh=0.48,
        import_pln_per_kwh=1.11,
    )
    assert settled.bill_pln == pytest.approx(0.23 * 0.48)
    assert settled.remainder_kwh == pytest.approx(-0.74)
    assert settled.prices_at_rce
    assert not settled.prices_at_g12
    assert settled.unsold_rce_pln_per_kwh == pytest.approx(0.48)


def test_open_hour_negative_meter_still_bills_positive_plan_at_rce() -> None:
    settled = settle_seller_hour(
        0.23,
        current_balance_kwh=-0.10,
        rce_pln_per_kwh=0.48,
        import_pln_per_kwh=1.11,
    )
    assert settled.bill_pln == pytest.approx(0.23 * 0.48)
    assert settled.remainder_kwh == pytest.approx(0.33)
    assert not settled.prices_at_g12


def test_negative_plan_bills_whole_deficit_at_g12_even_if_meter_positive() -> None:
    settled = settle_seller_hour(
        -1.0,
        current_balance_kwh=0.4,
        rce_pln_per_kwh=0.48,
        import_pln_per_kwh=1.11,
    )
    assert settled.bill_pln == pytest.approx(-1.11)
    assert settled.prices_at_g12
    assert settled.unsold_rce_pln_per_kwh is None
    assert settled.remainder_kwh == pytest.approx(-1.4)


def test_negative_rce_on_positive_balance_bills_zero() -> None:
    settled = settle_seller_hour(
        0.23,
        current_balance_kwh=0.97,
        rce_pln_per_kwh=-0.10,
        import_pln_per_kwh=1.11,
    )
    assert settled.bill_pln == pytest.approx(0.0)
    assert settled.unsold_rce_pln_per_kwh == pytest.approx(0.0)


def test_horizon_sum() -> None:
    s = total_cashflow_pln_for_horizon(
        [
            (1.0, 0.5, 0.5),
            (-1.0, 0.5, 2.0),
        ]
    )
    assert s == pytest.approx(0.5 - 2.0)
