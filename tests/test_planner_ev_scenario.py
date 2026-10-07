"""EV jest osobno od load: MILP rezerwuje je z PV, nie dobija braku z sieci ani z baterii."""

from __future__ import annotations

import pytest

from planner.battery import BatteryParams
from planner.hour_remainder import balance_rhs_kwh, green_export_cap_rhs_kwh
from planner.models import HourInputs, HourPlan
from planner.optimizer import optimize_horizon
from planner.policy_output import map_hour_to_exec_mode


def _hour(
    hour: int,
    *,
    pv: float,
    load: float,
    rce: float,
    ev: float = 0.0,
    import_pln: float = 1.11,
) -> HourInputs:
    return HourInputs(
        date="2026-10-01",
        hour=hour,
        load_kwh=load,
        load_base_kwh=load,
        load_kwh_p25=load,
        load_kwh_p75=load,
        ev_load_kwh=ev,
        pv_kwh=pv,
        pv_kwh_p10=pv,
        pv_kwh_p90=pv,
        import_pln_per_kwh=import_pln,
        export_pln_per_kwh=rce,
    )


def test_balance_reserves_ev_from_pv_and_drops_the_excess() -> None:
    house = _hour(12, pv=4.8, load=1.1, rce=0.0, ev=3.0)
    bare = house.model_copy(update={"ev_load_kwh": 0.0})
    assert balance_rhs_kwh(house) == pytest.approx(balance_rhs_kwh(bare) + 3.0)
    assert green_export_cap_rhs_kwh(house) == pytest.approx(1.8)

    over = _hour(13, pv=4.0, load=1.5, rce=0.2, ev=11.0)
    no_pv = over.model_copy(update={"pv_kwh": 0.0, "ev_load_kwh": 0.0})
    assert balance_rhs_kwh(over) == pytest.approx(balance_rhs_kwh(no_pv))
    assert green_export_cap_rhs_kwh(over) == pytest.approx(0.0)

    eaten = house.model_copy(update={"ev_load_kwh": 4.0})
    flat = map_hour_to_exec_mode(
        HourPlan(
            date="2026-10-01",
            hour=12,
            target_net_kwh=1.0,
            expected_cashflow_pln=0.0,
            soc_start_pct=40.0,
            soc_end_pct=40.0,
            battery_delta_kwh=0.0,
        ),
        eaten,
    )
    assert flat.exec_mode != "export_pv_surplus"


def test_noon_ev_pushes_battery_charge_to_the_morning() -> None:
    """Południowe PV jest dla auta. Pusta bateria ładuje się rano, pod wieczorny RCE."""
    bp = BatteryParams(
        capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0
    )
    hours = [
        _hour(10, pv=5.0, load=0.5, rce=0.45),
        _hour(11, pv=5.0, load=0.5, rce=0.43),
        _hour(12, pv=4.8, load=1.1, rce=0.0, ev=3.0),
        _hour(13, pv=4.5, load=1.6, rce=0.19, ev=3.0),
        _hour(18, pv=0.0, load=0.5, rce=1.15),
        _hour(19, pv=0.0, load=0.5, rce=1.15),
    ]
    plan = optimize_horizon(hours, soc_start_pct=17.0, params=bp)
    by_hour = {hp.hour: hp for hp in plan.hours}

    morning_charge = by_hour[10].battery_delta_kwh + by_hour[11].battery_delta_kwh
    assert morning_charge > 4.0
    assert by_hour[10].target_net_kwh < 1.5
    assert by_hour[12].battery_delta_kwh < 2.0
    assert by_hour[18].battery_delta_kwh < -1.0


def test_ev_above_pv_does_not_import_or_discharge_for_the_car() -> None:
    bp = BatteryParams(
        capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0
    )
    hours = [
        _hour(9, pv=5.0, load=0.3, rce=0.12),
        _hour(10, pv=5.0, load=0.3, rce=0.12, ev=11.0),
    ]
    plan = optimize_horizon(hours, soc_start_pct=50.0, params=bp)
    h9, h10 = plan.hours
    assert h9.battery_delta_kwh < 1.0
    assert h10.battery_delta_kwh > -0.5
    assert h10.target_net_kwh > -1.0
