"""Testy compute_export_profit_pace_w."""

from __future__ import annotations

import pytest

from guardian_logic import BalanceInputs, compute_export_profit_pace_w


def _inp(**kwargs) -> BalanceInputs:
    base = dict(
        remaining_kwh=0.0,
        time_to_end_s=3600.0,
        pv_w=0.0,
        consumption_w=500.0,
        soc_pct=60.0,
        p_inverter_w=5000.0,
        p_battery_w=3000.0,
        watts_per_percent=70.0,
    )
    base.update(kwargs)
    return BalanceInputs(**base)


def test_pace_w_with_ten_percent_margin() -> None:
    # 5 kWh above floor, 1 h left → 5 kW × 1.1 = 5.5 kW, cap 3 kW battery
    w = compute_export_profit_pace_w(
        _inp(soc_pct=60.0, time_to_end_s=3600.0),
        floor_pct=10.0,
        capacity_kwh=10.0,
        pace_margin=1.1,
        taper_threshold_pct=22.0,
        taper_max_w=300.0,
        plan_discharge_pct=100,
        min_discharge_pct=2,
    )
    assert w == pytest.approx(3000.0)


def test_pace_w_taper_below_threshold() -> None:
    # 0.8 kWh above floor, 40 min → 1.32 kW; taper 300 W wins
    w = compute_export_profit_pace_w(
        _inp(soc_pct=18.0, time_to_end_s=2400.0),
        floor_pct=10.0,
        capacity_kwh=10.0,
        pace_margin=1.1,
        taper_threshold_pct=22.0,
        taper_max_w=300.0,
        plan_discharge_pct=100,
        min_discharge_pct=2,
    )
    assert w == pytest.approx(300.0)
