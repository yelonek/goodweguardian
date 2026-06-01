"""Testy logiki guardiana (Flappy Bird / decide_watchdog)."""

import pytest

from guardian_logic import (
    BalanceInputs,
    WatchdogConfig,
    decide_watchdog,
    power_needed_kw,
)


class TestPowerNeededKw:
    def test_basic(self) -> None:
        assert power_needed_kw(0.5, 1800) == pytest.approx(1.0)
        assert power_needed_kw(-0.5, 1800) == pytest.approx(1.0)

    def test_small_time(self) -> None:
        assert power_needed_kw(0.01, 60) == pytest.approx(0.6)

    def test_below_threshold(self) -> None:
        assert power_needed_kw(0.005, 60) == pytest.approx(0.3)


class TestWatchdogPolicy:
    def test_deficit_recovery_triggers_immediately(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = -0.5
        default_inputs.time_to_end_s = 2400
        d = decide_watchdog(default_inputs, cfg=WatchdogConfig())
        assert d.write_slot is True
        assert d.mode == "discharge"
        assert d.reason in ("deficit_recovery", "deficit_max_cap", "deficit_min_assist")

    def test_flappy_buffer_build_when_pv_surplus(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = 0.02
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 3000.0
        default_inputs.consumption_w = 1500.0
        cfg = WatchdogConfig(
            soak_target_kwh=0.1,
            flappy_buffer_discharge_pct=1,
        )
        d = decide_watchdog(
            default_inputs,
            cfg=cfg,
            minute_of_hour=3,
        )
        assert d.write_slot is True
        assert d.power_pct == 1
        assert d.mode == "discharge"
        assert d.reason == "flappy_buffer_build"

    def test_flappy_neutral_when_pv_not_above_consumption(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = 0.02
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 1000.0
        default_inputs.consumption_w = 2000.0
        d = decide_watchdog(
            default_inputs,
            cfg=WatchdogConfig(),
            minute_of_hour=3,
        )
        assert d.write_slot is False
        assert d.reason == "flappy_neutral"

    def test_flappy_buffer_hold_when_target_reached(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = 0.12
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 3000.0
        default_inputs.consumption_w = 1000.0
        d = decide_watchdog(
            default_inputs,
            cfg=WatchdogConfig(),
            minute_of_hour=3,
        )
        assert d.write_slot is False
        assert d.reason == "flappy_buffer_hold"

    def test_flappy_build_works_late_in_hour(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = 0.02
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 3000.0
        default_inputs.consumption_w = 1000.0
        d = decide_watchdog(
            default_inputs,
            cfg=WatchdogConfig(),
            minute_of_hour=45,
        )
        assert d.write_slot is True
        assert d.reason == "flappy_buffer_build"

    def test_flappy_skips_low_soc(self, default_inputs: BalanceInputs) -> None:
        default_inputs.remaining_kwh = 0.05
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 3000.0
        default_inputs.consumption_w = 1000.0
        default_inputs.soc_pct = 20.0
        cfg = WatchdogConfig(
            soc_low_threshold_pct=22.0,
            soc_low_defense_release_remaining_kwh=0.0,
        )
        d = decide_watchdog(
            default_inputs,
            cfg=cfg,
            minute_of_hour=3,
        )
        assert d.write_slot is True
        assert d.reason == "soc_low_defense_hold"

    def test_flappy_skips_when_soc_full_band(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = -0.01
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 3000.0
        default_inputs.consumption_w = 1000.0
        default_inputs.soc_pct = 99.6
        cfg = WatchdogConfig(
            soc_full_threshold_pct=99.5,
            soc_full_defense_release_power_kw=0.5,
        )
        d = decide_watchdog(
            default_inputs,
            cfg=cfg,
            minute_of_hour=3,
        )
        assert d.write_slot is True
        assert d.reason == "soc_full_defense_hold"

    def test_deficit_recovery_when_needed(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = -0.06
        default_inputs.time_to_end_s = 300
        d = decide_watchdog(
            default_inputs,
            cfg=WatchdogConfig(grid_export_bias_w=150.0),
        )
        assert d.write_slot is True
        assert d.enabled is True
        assert d.mode == "discharge"
        assert d.reason in ("deficit_recovery", "deficit_min_assist", "deficit_max_cap")

    def test_end_hour_soak_when_export_surplus_high(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = 0.35
        default_inputs.time_to_end_s = 300.0
        default_inputs.pv_w = 5000.0
        default_inputs.consumption_w = 800.0
        d = decide_watchdog(
            default_inputs,
            cfg=WatchdogConfig(end_hour_window_s=600, end_hour_max_remaining_kwh=0.2),
        )
        assert d.write_slot is True
        assert d.mode == "charge"
        assert d.reason == "end_hour_battery_soak"

    def test_continuous_soak_when_surplus_above_trigger(
        self, default_inputs: BalanceInputs
    ) -> None:
        # nadwyżka PV + bilans > trigger 0.2, poza oknem końca godziny → soak ciągły (CHARGE).
        default_inputs.remaining_kwh = 0.3
        default_inputs.time_to_end_s = 2400.0
        default_inputs.pv_w = 5000.0
        default_inputs.consumption_w = 800.0
        cfg = WatchdogConfig(soak_target_kwh=0.1, soak_trigger_kwh=0.2)
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.write_slot is True
        assert d.mode == "charge"
        assert d.reason == "continuous_battery_soak"

    def test_continuous_soak_holds_in_deadband(
        self, default_inputs: BalanceInputs
    ) -> None:
        # bilans w paśmie [0.1, 0.2] przy nadwyżce → hold (bez charge i bez discharge).
        default_inputs.remaining_kwh = 0.15
        default_inputs.time_to_end_s = 2400.0
        default_inputs.pv_w = 5000.0
        default_inputs.consumption_w = 800.0
        cfg = WatchdogConfig(soak_target_kwh=0.1, soak_trigger_kwh=0.2)
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.write_slot is False
        assert d.mode == "neutral"
        assert d.reason == "flappy_buffer_hold"

    def test_continuous_soak_skipped_without_pv_surplus(
        self, default_inputs: BalanceInputs
    ) -> None:
        # bilans > trigger, ale brak nadwyżki PV → soak ciągły nie rusza, hold.
        default_inputs.remaining_kwh = 0.3
        default_inputs.time_to_end_s = 2400.0
        default_inputs.pv_w = 700.0
        default_inputs.consumption_w = 800.0
        cfg = WatchdogConfig(soak_target_kwh=0.1, soak_trigger_kwh=0.2)
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.write_slot is False
        assert d.reason == "flappy_buffer_hold"

    def test_deficit_never_commands_charge_with_pv_surplus(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = -0.01
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 9000
        default_inputs.consumption_w = 1000
        cfg = WatchdogConfig(min_discharge_assist_pct=0)
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.write_slot is False
        assert d.reason == "deficit_no_headroom"

    def test_deficit_max_cap_when_unrecoverable(
        self, default_inputs: BalanceInputs
    ) -> None:
        # PV=7kW → max discharge 1.2kW; -0.3kWh w 600s wymaga 1.8kW > cap
        default_inputs.remaining_kwh = -0.3
        default_inputs.time_to_end_s = 600
        default_inputs.pv_w = 7000.0
        default_inputs.consumption_w = 7500.0
        default_inputs.p_inverter_w = 8200.0
        d = decide_watchdog(
            default_inputs,
            cfg=WatchdogConfig(recoverable_fraction=0.9),
        )
        assert d.write_slot is True
        assert d.reason == "deficit_max_cap"
        assert d.power_pct == 17  # 1200W / 70W/%

    def test_deficit_min_assist_when_high_pv(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = -0.06
        default_inputs.time_to_end_s = 300
        default_inputs.pv_w = 5000
        default_inputs.consumption_w = 800
        d = decide_watchdog(
            default_inputs,
            cfg=WatchdogConfig(grid_export_bias_w=150.0),
        )
        assert d.write_slot is True
        assert d.power_pct == 1
        assert d.mode == "discharge"
        assert d.reason == "deficit_min_assist"

    def test_flappy_hold_while_hour_balance_positive(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = 0.12
        default_inputs.time_to_end_s = 1800
        default_inputs.pv_w = 9000
        default_inputs.consumption_w = 6000
        d = decide_watchdog(default_inputs, cfg=WatchdogConfig())
        assert d.write_slot is False
        assert d.reason == "flappy_buffer_hold"

    def test_deficit_no_headroom_when_assist_disabled(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = -0.06
        default_inputs.time_to_end_s = 300
        default_inputs.pv_w = 5000
        default_inputs.consumption_w = 800
        d = decide_watchdog(
            default_inputs,
            cfg=WatchdogConfig(min_discharge_assist_pct=0),
        )
        assert d.write_slot is False
        assert d.reason == "deficit_no_headroom"

    def test_soc_full_defense_holds_charge_early_while_net_export(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = 0.05
        cfg = WatchdogConfig(
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_release_power_kw=0.5,
        )
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.write_slot is True
        assert d.power_pct == -1
        assert d.reason == "soc_full_defense_hold"

    def test_soc_full_defense_releases_when_balance_power_above_threshold(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = -0.5
        cfg = WatchdogConfig(
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_release_power_kw=0.5,
        )
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.reason != "soc_full_defense_hold"

    def test_soc_full_defense_hour_start_without_carryover_flag(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 3500
        default_inputs.remaining_kwh = 0.0
        cfg = WatchdogConfig(
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_carryover_minutes=5,
        )
        d = decide_watchdog(
            default_inputs,
            cfg=cfg,
            minute_of_hour=0,
            soc_full_defense_carryover=False,
        )
        assert d.write_slot is True
        assert d.reason == "soc_full_defense_hold"

    def test_soc_full_defense_carryover_first_minutes_after_hour_reset(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 3500
        default_inputs.remaining_kwh = 0.0
        cfg = WatchdogConfig(
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_carryover_minutes=5,
        )
        d = decide_watchdog(
            default_inputs,
            cfg=cfg,
            minute_of_hour=2,
            soc_full_defense_carryover=True,
        )
        assert d.write_slot is True
        assert d.reason == "soc_full_defense_carryover"

    def test_soc_full_defense_carryover_not_active_without_flag(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 3500
        default_inputs.remaining_kwh = 0.0
        cfg = WatchdogConfig(
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_carryover_minutes=5,
        )
        d = decide_watchdog(
            default_inputs,
            cfg=cfg,
            minute_of_hour=2,
            soc_full_defense_carryover=False,
        )
        assert d.reason == "soc_full_defense_hold"

    def test_soc_full_defense_carryover_releases_on_net_import(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 3500
        default_inputs.remaining_kwh = -0.01
        cfg = WatchdogConfig(
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
        )
        d = decide_watchdog(
            default_inputs,
            cfg=cfg,
            minute_of_hour=2,
        )
        assert d.reason != "soc_full_defense_carryover"

    def test_soc_full_defense_last_minute_of_hour_uses_hold(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 45.0
        default_inputs.remaining_kwh = 0.0
        cfg = WatchdogConfig(
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_release_power_kw=1.0,
        )
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.write_slot is True
        assert d.reason == "soc_full_defense_hold"

    def test_soc_full_defense_tolerates_import_if_release_high(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = -0.1
        cfg = WatchdogConfig(
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_release_power_kw=1.0,
        )
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.reason == "soc_full_defense_hold"

    def test_soc_full_defense_releases_to_deficit_recovery(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 300.0
        default_inputs.remaining_kwh = -0.1
        cfg = WatchdogConfig(
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_release_power_kw=0.5,
        )
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.reason != "soc_full_defense_hold"
        assert d.mode == "discharge"

    def test_soc_low_defense_holds_while_remaining_above_hour_target(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 18.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = 0.05
        cfg = WatchdogConfig(
            soc_low_threshold_pct=22.0,
            soc_low_defense_charge_pct=-1,
            soc_low_defense_release_remaining_kwh=0.0,
        )
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.write_slot is True
        assert d.power_pct == -1
        assert d.reason == "soc_low_defense_hold"

    def test_soc_low_discharge_cap_uses_recent_average_regardless_of_balance(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 18.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = -0.20
        default_inputs.pv_w = 100.0
        default_inputs.consumption_w = 1000.0
        default_inputs.low_soc_discharge_target_w = 420.0
        cfg = WatchdogConfig(soc_low_threshold_pct=20.0)
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.write_slot is True
        assert d.power_pct == 6
        assert d.mode == "discharge"
        assert d.reason == "soc_low_discharge_cap"

    def test_soc_low_discharge_cap_allows_charging_when_pv_surplus(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 15.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = 3.0
        default_inputs.pv_w = 6000.0
        default_inputs.consumption_w = 500.0
        default_inputs.low_soc_discharge_target_w = 500.0
        cfg = WatchdogConfig(soc_low_threshold_pct=20.0)
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.write_slot is False
        assert d.mode == "neutral"
        assert d.reason == "soc_low_pv_surplus_no_discharge"

    def test_soc_low_pv_surplus_prioritizes_negative_hour_balance(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 15.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = -0.20
        default_inputs.pv_w = 6000.0
        default_inputs.consumption_w = 500.0
        default_inputs.low_soc_discharge_target_w = 500.0
        cfg = WatchdogConfig(soc_low_threshold_pct=20.0, min_discharge_assist_pct=1)
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.write_slot is True
        assert d.power_pct == 1
        assert d.mode == "discharge"
        assert d.reason == "soc_low_pv_surplus_balance_priority"

    def test_soc_low_discharge_cap_is_limited_by_load_deficit(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 18.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = -0.20
        default_inputs.pv_w = 700.0
        default_inputs.consumption_w = 1000.0
        default_inputs.low_soc_discharge_target_w = 700.0
        cfg = WatchdogConfig(soc_low_threshold_pct=20.0)
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.write_slot is True
        assert d.power_pct == 4
        assert d.reason == "soc_low_discharge_cap"

    def test_soc_low_discharge_cap_can_override_other_eco_slot(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 18.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = 0.10
        default_inputs.pv_w = 100.0
        default_inputs.consumption_w = 1000.0
        default_inputs.low_soc_discharge_target_w = 350.0
        default_inputs.other_eco_slot_active = True
        cfg = WatchdogConfig(soc_low_threshold_pct=20.0)
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.write_slot is True
        assert d.power_pct == 5
        assert d.reason == "soc_low_discharge_cap"

    def test_night_soc_reserve_has_priority_over_low_soc_discharge_cap(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 18.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = -0.20
        default_inputs.pv_w = 100.0
        default_inputs.consumption_w = 1000.0
        default_inputs.low_soc_discharge_target_w = 420.0
        cfg = WatchdogConfig(
            soc_low_threshold_pct=20.0,
            soc_night_reserve_pct=20.0,
            soc_night_reserve_charge_pct=-1,
            night_reserve_hours=frozenset({22, 23, 0, 1, 2, 3, 4, 5}),
        )
        d = decide_watchdog(
            default_inputs,
            cfg=cfg,
            hour_of_day=3,
        )
        assert d.write_slot is True
        assert d.power_pct == -1
        assert d.mode == "charge"
        assert d.reason == "night_soc_reserve_hold"

    def test_soc_low_defense_releases_when_remaining_at_or_below_hour_target(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 18.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = -0.01
        cfg = WatchdogConfig(
            soc_low_threshold_pct=22.0,
            soc_low_defense_charge_pct=-1,
            soc_low_defense_release_remaining_kwh=0.0,
        )
        d = decide_watchdog(default_inputs, cfg=cfg)
        assert d.reason != "soc_low_defense_hold"

    def test_night_soc_reserve_holds_in_night_hour(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 40.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = -0.2
        cfg = WatchdogConfig(
            soc_night_reserve_pct=40.0,
            soc_night_reserve_charge_pct=-1,
            night_reserve_hours=frozenset({22, 23, 0, 1, 2, 3, 4, 5}),
        )
        d = decide_watchdog(
            default_inputs,
            cfg=cfg,
            hour_of_day=3,
        )
        assert d.write_slot is True
        assert d.enabled is True
        assert d.power_pct == -1
        assert d.mode == "charge"
        assert d.reason == "night_soc_reserve_hold"

    def test_night_soc_reserve_inactive_outside_night_hours(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 40.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = -0.2
        cfg = WatchdogConfig(
            soc_night_reserve_pct=40.0,
            soc_night_reserve_charge_pct=-1,
            night_reserve_hours=frozenset({22, 23, 0, 1, 2, 3, 4, 5}),
        )
        d = decide_watchdog(
            default_inputs,
            cfg=cfg,
            hour_of_day=10,
        )
        assert d.reason != "night_soc_reserve_hold"

    def test_night_soc_reserve_inactive_when_soc_above_threshold(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 55.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = -0.2
        cfg = WatchdogConfig(
            soc_night_reserve_pct=40.0,
            soc_night_reserve_charge_pct=-1,
            night_reserve_hours=frozenset({22, 23, 0, 1, 2, 3, 4, 5}),
        )
        d = decide_watchdog(
            default_inputs,
            cfg=cfg,
            hour_of_day=3,
        )
        assert d.reason != "night_soc_reserve_hold"
