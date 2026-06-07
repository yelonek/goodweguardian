"""Guardian: bilans względem target_net_kwh z planu."""

import pytest

from guardian_logic import BalanceInputs, WatchdogConfig, decide_watchdog


def test_deficit_vs_plan_target_triggers_recovery() -> None:
    """actual +0.3, target +2.0 → delta −1.7 → korekta deficytu względem planu."""
    inp = BalanceInputs(
        remaining_kwh=-1.7,
        time_to_end_s=1800.0,
        pv_w=500.0,
        consumption_w=800.0,
        soc_pct=60.0,
        p_inverter_w=5000.0,
        p_battery_w=3000.0,
    )
    decision = decide_watchdog(inp, cfg=WatchdogConfig(), hour_of_day=12, minute_of_hour=30)
    assert decision.write_slot is True
    assert decision.mode == "discharge"
