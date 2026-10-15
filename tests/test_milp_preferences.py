"""Preferencje MILP: defer dzienny, rozkład ładowania, derating >90%."""

from __future__ import annotations

import pytest

from planner.battery import BatteryParams
from planner.milp_preferences import CHARGE_DERATE_KW
from planner.models import HourInputs
from planner.optimizer import optimize_horizon
import planner.optimizer as opt_mod


@pytest.fixture(autouse=True)
def _deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)


def _hp(
    hour: int,
    *,
    load: float,
    pv: float,
    imp: float,
    exp: float,
    date: str = "2026-10-11",
) -> HourInputs:
    return HourInputs(
        date=date,
        hour=hour,
        load_kwh=load,
        pv_kwh=pv,
        import_pln_per_kwh=imp,
        export_pln_per_kwh=exp,
    )


def test_day_equal_import_defers_grid_charge_to_later_hour() -> None:
    """11 X styl: 13 i 14 po 0,59 — ładuj później, zostaw 13 na PV."""
    bp = BatteryParams(
        capacity_kwh=10.0,
        soc_min_pct=10.0,
        soc_max_pct=100.0,
        max_power_kwh_per_h=5.0,
        eta=0.94,
    )
    hours = [
        _hp(13, load=1.0, pv=0.8, imp=0.59, exp=0.48),
        _hp(14, load=1.0, pv=2.4, imp=0.59, exp=0.19),
        _hp(20, load=3.5, pv=0.0, imp=1.11, exp=0.40),
    ]
    res = optimize_horizon(hours, soc_start_pct=64.0, params=bp)
    assert res.hours[0].battery_delta_kwh < res.hours[1].battery_delta_kwh
    assert res.hours[0].target_net_kwh > res.hours[1].target_net_kwh - 0.05


def test_night_equal_import_spreads_charge_across_hours() -> None:
    """Przy remisie nocnym nie wbijaj całego ładunku w jedną godzinę."""
    bp = BatteryParams(
        capacity_kwh=10.0,
        soc_min_pct=10.0,
        soc_max_pct=100.0,
        max_power_kwh_per_h=5.0,
        eta=0.94,
    )
    hours = [
        _hp(22, load=0.4, pv=0.0, imp=0.59, exp=0.05, date="2026-10-11"),
        _hp(23, load=0.4, pv=0.0, imp=0.59, exp=0.05, date="2026-10-11"),
        _hp(0, load=0.4, pv=0.0, imp=0.59, exp=0.05, date="2026-10-12"),
        _hp(1, load=0.4, pv=0.0, imp=0.59, exp=0.05, date="2026-10-12"),
        _hp(8, load=4.0, pv=0.0, imp=1.11, exp=0.30, date="2026-10-12"),
    ]
    res = optimize_horizon(hours, soc_start_pct=20.0, params=bp)
    night = res.hours[:4]
    charges = [max(0.0, hp.battery_delta_kwh) for hp in night]
    total = sum(charges)
    assert total > 3.0
    assert max(charges) < total * 0.55


def test_charge_derate_above_90_soc_caps_power() -> None:
    """Start 92%: ładowanie do full w strefie derate ≤ ~2 kW (nie 5)."""
    bp = BatteryParams(
        capacity_kwh=25.0,
        soc_min_pct=10.0,
        soc_max_pct=100.0,
        max_power_kwh_per_h=5.0,
        eta=0.94,
    )
    hours = [_hp(12, load=0.0, pv=0.0, imp=0.20, exp=0.01)]
    for hour in range(13, 18):
        hours.append(_hp(hour, load=5.0, pv=0.0, imp=1.20, exp=0.01))
    res = optimize_horizon(hours, soc_start_pct=92.0, params=bp)
    assert res.hours[0].battery_delta_kwh <= CHARGE_DERATE_KW + 0.05
    assert res.hours[0].battery_delta_kwh > 0.5
    assert res.hours[0].soc_end_pct <= 92.0 + (CHARGE_DERATE_KW + 0.05) / bp.capacity_kwh * 100.0 + 1.0
