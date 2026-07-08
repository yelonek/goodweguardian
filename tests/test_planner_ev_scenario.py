"""Scenariusz 9→10: deklaracja EV zmienia plan ładowania baterii."""

from __future__ import annotations

import pytest

from planner.battery import BatteryParams
from planner.models import HourInputs
from planner.optimizer import optimize_horizon


def _morning_ev_case(*, ev_at_10: float) -> list[HourInputs]:
    d = "2026-06-11"
    base = 0.3
    pv = 5.0
    rce = 0.12
    return [
        HourInputs(
            date=d,
            hour=9,
            load_kwh=base,
            pv_kwh=pv,
            pv_kwh_p10=pv * 0.8,
            pv_kwh_p90=pv * 1.1,
            load_kwh_p25=base,
            load_kwh_p75=base,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=rce,
        ),
        HourInputs(
            date=d,
            hour=10,
            load_kwh=base + ev_at_10,
            ev_load_kwh=ev_at_10,
            load_base_kwh=base,
            pv_kwh=pv,
            pv_kwh_p10=pv * 0.8,
            pv_kwh_p90=pv * 1.1,
            load_kwh_p25=base,
            load_kwh_p75=base + ev_at_10,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=rce,
        ),
    ]


def test_ev_at_10_increases_battery_charge_at_9() -> None:
    bp = BatteryParams(
        capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0
    )
    without = optimize_horizon(_morning_ev_case(ev_at_10=0.0), soc_start_pct=50.0, params=bp)
    with_ev = optimize_horizon(_morning_ev_case(ev_at_10=11.0), soc_start_pct=50.0, params=bp)

    h9_without = without.hours[0]
    h9_with = with_ev.hours[0]

    assert h9_with.battery_delta_kwh > h9_without.battery_delta_kwh + 1.0
    assert h9_with.battery_delta_kwh > 0.5
