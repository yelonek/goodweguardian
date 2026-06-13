"""Testy modułu economics — spójność z logiką KPI (depozyt − rachunek)."""

from __future__ import annotations

import pytest

from economics import (
    battery_wear_pln_for_hour,
    cashflow_pln_for_hour,
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


def test_battery_wear_full_cycle() -> None:
  assert battery_wear_pln_for_hour(1.0, 1.0, cycle_cost_pln=0.10) == pytest.approx(0.10)
  assert battery_wear_pln_for_hour(1.0, 0.0, cycle_cost_pln=0.10) == pytest.approx(0.05)
  assert battery_wear_pln_for_hour(0.0, 1.0, cycle_cost_pln=0.10) == pytest.approx(0.05)
  assert battery_wear_pln_for_hour(1.0, 1.0, cycle_cost_pln=0.0) == 0.0


def test_horizon_sum() -> None:
    s = total_cashflow_pln_for_horizon(
        [
            (1.0, 0.5, 0.5),
            (-1.0, 0.5, 2.0),
        ]
    )
    assert s == pytest.approx(0.5 - 2.0)
