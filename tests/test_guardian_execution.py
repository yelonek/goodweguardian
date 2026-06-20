"""Egzekucja exec_mode z planera."""

import pytest

from guardian_execution import decide_plan_execution
from guardian_logic import BalanceInputs, WatchdogConfig
from planner.models import HourPolicyParams, HourPolicyRow


def _inp(**kwargs) -> BalanceInputs:
    base = dict(
        remaining_kwh=0.0,
        time_to_end_s=2400.0,
        pv_w=3000.0,
        consumption_w=1500.0,
        soc_pct=60.0,
        p_inverter_w=5000.0,
        p_battery_w=3000.0,
    )
    base.update(kwargs)
    return BalanceInputs(**base)


def _row(exec_mode: str, **params) -> HourPolicyRow:
    base = dict(target_net_kwh=0.0, battery_delta_kwh=0.0, soc_end_pct=55.0)
    base.update(params)
    p = HourPolicyParams(**base)
    return HourPolicyRow(
        date="2026-06-10",
        hour=12,
        exec_mode=exec_mode,  # type: ignore[arg-type]
        params=p,
    )


def test_export_pv_surplus_steady_discharge() -> None:
    d = decide_plan_execution(
        _inp(remaining_kwh=0.5, pv_w=4000.0),
        _row("export_pv_surplus", target_net_kwh=2.0),
        cfg=WatchdogConfig(),
    )
    assert d.write_slot is True
    assert d.power_pct == 1
    assert d.reason == "export_pv_surplus"


def test_export_pv_surplus_deficit_only_below_zero() -> None:
    d = decide_plan_execution(
        _inp(remaining_kwh=-0.4, pv_w=500.0, consumption_w=2000.0),
        _row("export_pv_surplus"),
        cfg=WatchdogConfig(),
    )
    assert d.write_slot is True
    assert d.mode == "discharge"
    assert "deficit" in d.reason


def test_import_grid_charge_1_soc_10() -> None:
    d = decide_plan_execution(
        _inp(remaining_kwh=-1.0),
        _row("import_grid", target_net_kwh=-1.5),
        cfg=WatchdogConfig(),
    )
    assert d.write_slot is True
    assert d.power_pct == -1
    assert d.slot_soc_pct == 10
    assert d.reason == "import_grid"


def test_neutral_waits_when_load_above_pv_and_above_target() -> None:
    d = decide_plan_execution(
        _inp(remaining_kwh=2.0, pv_w=800.0, consumption_w=2000.0),
        _row("neutral", target_net_kwh=2.0),
        cfg=WatchdogConfig(),
    )
    assert d.write_slot is False
    assert d.reason == "neutral_wait_above_target"


def test_charge_grid_active_charge() -> None:
    d = decide_plan_execution(
        _inp(soc_pct=40.0),
        _row(
            "charge_grid",
            target_soc_pct=80.0,
            charge_pct=15,
            allow_grid_charge=True,
        ),
        cfg=WatchdogConfig(),
    )
    assert d.write_slot is True
    assert d.power_pct == -15
    assert d.slot_soc_pct == 80


def test_export_profit_at_full_soc_not_blocked_by_full_defense() -> None:
    d = decide_plan_execution(
        _inp(soc_pct=99.8, remaining_kwh=1.0),
        _row("export_profit", soc_floor_pct=20.0, discharge_pct=15),
        cfg=WatchdogConfig(soc_full_threshold_pct=99.5),
    )
    assert d.reason == "export_profit_pace"
    assert d.power_pct == 15


def test_export_profit_skips_low_soc_defense() -> None:
    """Poniżej progu: taper LFP, nie soc_low_discharge_cap."""
    d = decide_plan_execution(
        _inp(
            soc_pct=18.0,
            pv_w=100.0,
            consumption_w=1000.0,
            p_inverter_w=8200.0,
            p_battery_w=5200.0,
            watts_per_percent=72.0,
            low_soc_discharge_target_w=520.0,
        ),
        _row("export_profit", soc_floor_pct=10.0, discharge_pct=100),
        cfg=WatchdogConfig(soc_low_threshold_pct=22.0),
    )
    assert d.reason == "export_profit_pace"
    assert d.reason != "soc_low_discharge_cap"
    assert d.power_pct == 14  # min(1000 W, full) ≈ 1000 / 72


def test_export_profit_full_power_above_threshold() -> None:
    d = decide_plan_execution(
        _inp(
            soc_pct=25.0,
            p_inverter_w=8200.0,
            p_battery_w=5200.0,
            watts_per_percent=72.0,
            low_soc_discharge_target_w=520.0,
        ),
        _row("export_profit", soc_floor_pct=10.0, discharge_pct=46),
        cfg=WatchdogConfig(soc_low_threshold_pct=20.0),
    )
    assert d.reason == "export_profit_pace"
    assert d.power_pct == 46


def test_export_profit_pace_caps_at_plan_discharge_pct() -> None:
    d = decide_plan_execution(
        _inp(soc_pct=80.0, time_to_end_s=3600.0, pv_w=0.0, consumption_w=500.0),
        _row("export_profit", soc_floor_pct=10.0, discharge_pct=10),
        cfg=WatchdogConfig(soc_low_threshold_pct=22.0),
    )
    assert d.reason == "export_profit_pace"
    assert d.power_pct == 10


def test_export_pv_surplus_at_full_soc_uses_full_defense() -> None:
    d = decide_plan_execution(
        _inp(soc_pct=99.8, remaining_kwh=1.0),
        _row("export_pv_surplus"),
        cfg=WatchdogConfig(soc_full_threshold_pct=99.5),
    )
    assert d.reason == "soc_full_defense_hold"
    assert d.power_pct == -1


def test_import_grid_low_soc_skips_low_defense() -> None:
    d = decide_plan_execution(
        _inp(soc_pct=15.0, remaining_kwh=-0.5),
        _row("import_grid", target_net_kwh=-1.0),
        cfg=WatchdogConfig(soc_low_threshold_pct=22.0, soc_low_defense_charge_pct=-1),
    )
    assert d.reason == "import_grid"
    assert d.power_pct == -1


def test_neutral_low_soc_uses_low_defense() -> None:
    d = decide_plan_execution(
        _inp(soc_pct=15.0, remaining_kwh=0.5),
        _row("neutral", target_net_kwh=0.0),
        cfg=WatchdogConfig(
            soc_low_threshold_pct=22.0,
            soc_low_defense_charge_pct=-1,
            soc_low_defense_release_remaining_kwh=0.0,
        ),
    )
    assert d.reason == "soc_low_defense_hold"


def test_neutral_at_soc_floor_with_load_deficit_charges_not_discharges() -> None:
    """SOC na minimum + plan ładuje → deficyt loadu z sieci, nie rozładowanie baterii."""
    d = decide_plan_execution(
        _inp(
            soc_pct=10.0,
            remaining_kwh=0.34,
            pv_w=3100.0,
            consumption_w=3908.0,
            low_soc_discharge_target_w=1200.0,
        ),
        _row("neutral", target_net_kwh=0.42, battery_delta_kwh=4.15),
        cfg=WatchdogConfig(
            soc_low_threshold_pct=20.0,
            soc_low_defense_charge_pct=-1,
        ),
    )
    assert d.mode == "charge"
    assert d.power_pct == -1
    assert d.reason == "soc_low_grid_covers_load"


def test_neutral_low_soc_pv_surplus_soak_when_plan_charges() -> None:
    """Nadwyżka PV + plan ładuje → CHARGE -1%, nie eksport DISCHARGE 1%."""
    d = decide_plan_execution(
        _inp(
            soc_pct=15.0,
            remaining_kwh=0.05,
            pv_w=5950.0,
            consumption_w=1335.0,
            low_soc_discharge_target_w=1200.0,
        ),
        _row("neutral", target_net_kwh=1.49, battery_delta_kwh=4.15),
        cfg=WatchdogConfig(
            soc_low_threshold_pct=20.0,
            soc_low_defense_charge_pct=-1,
        ),
    )
    assert d.mode == "charge"
    assert d.power_pct == -1
    assert d.reason == "soc_low_pv_soak"


def test_export_profit_respects_soc_floor() -> None:
    d = decide_plan_execution(
        _inp(soc_pct=12.0),
        _row("export_profit", soc_floor_pct=15.0, discharge_pct=20),
        cfg=WatchdogConfig(soc_low_threshold_pct=10.0),
    )
    assert d.power_pct == 1
    assert d.reason == "export_profit_soc_floor"
