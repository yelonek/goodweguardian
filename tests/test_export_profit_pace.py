"""Testy compute_export_profit_pace_w i export_profit low-SOC taper."""

from __future__ import annotations

import pytest

from guardian_logic import (
    BalanceInputs,
    compute_export_profit_pace_w,
    export_profit_low_soc_taper_max_w,
    load_cover_discharge_w,
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


def test_max_discharge_at_high_soc() -> None:
    w = compute_export_profit_pace_w(
        _inp(soc_pct=60.0),
        plan_discharge_pct=100,
        min_discharge_pct=2,
    )
    assert w == pytest.approx(3000.0)


def test_lfp_taper_caps_below_threshold() -> None:
    inp = _inp(soc_pct=15.0, low_soc_discharge_target_w=520.0)
    taper = export_profit_low_soc_taper_max_w(
        inp, threshold_pct=20.0, full_max_w=5200.0, lfp_cap_w=1500.0
    )
    assert taper == pytest.approx(1500.0)


def test_lfp_taper_covers_at_least_load() -> None:
    inp = _inp(soc_pct=15.0, low_soc_discharge_target_w=520.0)
    taper = export_profit_low_soc_taper_max_w(
        inp, threshold_pct=20.0, full_max_w=5200.0, lfp_cap_w=400.0
    )
    assert taper == pytest.approx(520.0)


def test_no_taper_above_threshold() -> None:
    inp = _inp(soc_pct=25.0, low_soc_discharge_target_w=520.0)
    assert (
        export_profit_low_soc_taper_max_w(
            inp, threshold_pct=20.0, full_max_w=5200.0, lfp_cap_w=1500.0
        )
        == 0.0
    )


def test_compute_applies_taper() -> None:
    w = compute_export_profit_pace_w(
        _inp(soc_pct=18.0),
        plan_discharge_pct=100,
        min_discharge_pct=2,
        taper_max_w=1000.0,
    )
    assert w == pytest.approx(1000.0)


def test_load_cover_from_consumption() -> None:
    inp = _inp(low_soc_discharge_target_w=None, consumption_w=480.0)
    assert load_cover_discharge_w(inp) == pytest.approx(480.0)
