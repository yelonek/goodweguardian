"""Testy compute_export_profit_pace_w i resolve_low_soc_taper_max_w."""

from __future__ import annotations

import pytest

from guardian_logic import BalanceInputs, compute_export_profit_pace_w, resolve_low_soc_taper_max_w


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


def test_max_discharge_above_low_soc_threshold() -> None:
    w = compute_export_profit_pace_w(
        _inp(soc_pct=60.0, time_to_end_s=3600.0),
        taper_threshold_pct=22.0,
        taper_max_w=500.0,
        plan_discharge_pct=100,
        min_discharge_pct=2,
    )
    assert w == pytest.approx(3000.0)


def test_taper_below_threshold() -> None:
    w = compute_export_profit_pace_w(
        _inp(soc_pct=18.0, time_to_end_s=2400.0),
        taper_threshold_pct=22.0,
        taper_max_w=500.0,
        plan_discharge_pct=100,
        min_discharge_pct=2,
    )
    assert w == pytest.approx(500.0)


def test_caps_at_plan_discharge_pct() -> None:
    w = compute_export_profit_pace_w(
        _inp(soc_pct=80.0, time_to_end_s=3600.0),
        taper_threshold_pct=22.0,
        taper_max_w=500.0,
        plan_discharge_pct=10,
        min_discharge_pct=2,
    )
    assert w == pytest.approx(700.0)


def test_resolve_taper_uses_low_soc_average() -> None:
    inp = _inp(soc_pct=19.0, low_soc_discharge_target_w=520.0, consumption_w=400.0)
    assert resolve_low_soc_taper_max_w(inp, threshold_pct=20.0) == pytest.approx(520.0)


def test_resolve_taper_falls_back_to_consumption_w() -> None:
    inp = _inp(soc_pct=19.0, low_soc_discharge_target_w=None, consumption_w=480.0)
    assert resolve_low_soc_taper_max_w(inp, threshold_pct=20.0) == pytest.approx(480.0)


def test_resolve_taper_off_above_threshold() -> None:
    inp = _inp(soc_pct=25.0, low_soc_discharge_target_w=520.0)
    assert resolve_low_soc_taper_max_w(inp, threshold_pct=20.0) == 0.0
