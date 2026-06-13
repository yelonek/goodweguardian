"""Guardian: egzekucja exec_mode (bez chase actual−target)."""

from guardian_execution import decide_plan_execution
from guardian_logic import BalanceInputs, WatchdogConfig
from planner.models import HourPolicyParams, HourPolicyRow


def test_export_pv_surplus_no_chase_large_target() -> None:
    """actual=0, target=+3 → nadal 1% discharge, nie agresywna bateria."""
    inp = BalanceInputs(
        remaining_kwh=0.0,
        time_to_end_s=3500.0,
        pv_w=4000.0,
        consumption_w=1500.0,
        soc_pct=60.0,
        p_inverter_w=5000.0,
        p_battery_w=3000.0,
    )
    row = HourPolicyRow(
        date="2026-06-10",
        hour=12,
        exec_mode="export_pv_surplus",
        params=HourPolicyParams(
            target_net_kwh=3.0,
            battery_delta_kwh=0.0,
            soc_end_pct=55.0,
        ),
    )
    decision = decide_plan_execution(inp, row, cfg=WatchdogConfig())
    assert decision.write_slot is True
    assert decision.power_pct == 1
    assert decision.reason == "export_pv_surplus"
