"""Semantyka η_rt magazynu: √η na charge i discharge → cykl AC→AC = η_rt."""

from __future__ import annotations

import math

import pytest

from planner.battery import (
    BatteryParams,
    apply_battery_step,
    eta_one_way_from_rt,
    soc_kwh,
)


def test_eta_one_way_is_sqrt_of_rt() -> None:
    assert eta_one_way_from_rt(0.81) == pytest.approx(0.9)
    assert eta_one_way_from_rt(0.92) == pytest.approx(math.sqrt(0.92))


def test_round_trip_recovers_eta_rt_not_eta_squared() -> None:
    """1 kWh AC in → SOC → 1 kWh AC out: odzysk = η_rt (nie η_rt²)."""
    eta_rt = 0.81
    params = BatteryParams(
        capacity_kwh=10.0,
        eta=eta_rt,
        soc_min_pct=0.0,
        soc_max_pct=100.0,
        max_power_kwh_per_h=10.0,
    )
    start_pct = 50.0
    mid = apply_battery_step(start_pct, 1.0, params)
    assert mid is not None
    stored = soc_kwh(mid, params) - soc_kwh(start_pct, params)
    assert stored == pytest.approx(math.sqrt(eta_rt))

    # Ile AC trzeba oddać, żeby wrócić do startowego SOC:
    # delivered_from_cells = stored; ac_out = stored * η₁ = η_rt
    ac_out = stored * params.eta_one_way
    assert ac_out == pytest.approx(eta_rt)

    end = apply_battery_step(mid, -ac_out, params)
    assert end is not None
    assert end == pytest.approx(start_pct)


def test_battery_params_eta_one_way_property() -> None:
    p = BatteryParams(eta=0.64, max_power_kwh_per_h=5.0)
    assert p.eta_one_way == pytest.approx(0.8)
