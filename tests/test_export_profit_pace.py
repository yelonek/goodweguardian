"""Testy compute_export_profit_pace_w i sufitu mocy przy niskim SOC."""

from __future__ import annotations

import pytest

from guardian_logic import (
    BalanceInputs,
    WatchdogConfig,
    battery_discharge_cap_w,
    compute_export_profit_pace_w,
)


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


def _cap_cfg(**kwargs) -> WatchdogConfig:
    base = dict(
        soc_low_cap_soc_high_pct=20.0,
        soc_low_cap_soc_low_pct=10.0,
        soc_low_cap_w_high=1000.0,
        soc_low_cap_w_low=70.0,
    )
    base.update(kwargs)
    return WatchdogConfig(**base)


def test_max_discharge_at_high_soc() -> None:
    w = compute_export_profit_pace_w(
        _inp(soc_pct=60.0),
        plan_discharge_pct=100,
        min_discharge_pct=2,
    )
    assert w == pytest.approx(3000.0)


@pytest.mark.parametrize(
    ("soc_pct", "expected_w"),
    [
        (20.0, 1000.0),
        (15.0, 535.0),
        (11.0, 163.0),
        (10.0, 70.0),
        (9.0, 70.0),
    ],
)
def test_soc_low_cap_lerp_points(soc_pct: float, expected_w: float) -> None:
    cap = battery_discharge_cap_w(_inp(soc_pct=soc_pct), _cap_cfg())
    assert cap == pytest.approx(expected_w)


def test_no_cap_above_high_soc() -> None:
    assert battery_discharge_cap_w(_inp(soc_pct=25.0), _cap_cfg()) is None


def test_cap_does_not_boost_to_load() -> None:
    inp = _inp(soc_pct=12.0, consumption_w=500.0)
    cap = battery_discharge_cap_w(inp, _cap_cfg())
    assert cap == pytest.approx(256.0)
    assert cap < 500.0


def test_soc_low_cap_with_custom_w_high() -> None:
    inp = _inp(soc_pct=15.0)
    cfg = _cap_cfg(soc_low_cap_w_high=1500.0)
    cap = battery_discharge_cap_w(inp, cfg, full_max_w=5200.0)
    assert cap == pytest.approx(785.0)


def test_soc_low_cap_lower_w_high() -> None:
    inp = _inp(soc_pct=15.0)
    cfg = _cap_cfg(soc_low_cap_w_high=400.0)
    cap = battery_discharge_cap_w(inp, cfg, full_max_w=5200.0)
    assert cap == pytest.approx(235.0)


def test_no_cap_above_zone() -> None:
    inp = _inp(soc_pct=25.0)
    cfg = _cap_cfg(soc_low_cap_w_high=1500.0)
    assert battery_discharge_cap_w(inp, cfg, full_max_w=5200.0) is None


def test_compute_applies_cap_without_min_discharge_boost() -> None:
    w = compute_export_profit_pace_w(
        _inp(soc_pct=18.0),
        plan_discharge_pct=100,
        min_discharge_pct=2,
        taper_max_w=814.0,
    )
    assert w == pytest.approx(814.0)
