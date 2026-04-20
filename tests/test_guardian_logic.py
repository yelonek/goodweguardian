"""Testy logiki guardiana (BalanceInputs → BalanceOutput)."""

import pytest

from guardian_logic import (
    BalanceInputs,
    WatchdogConfig,
    WatchdogState,
    decide_watchdog,
    compute_intervention,
    power_needed_kw,
    tolerance_pct,
)


class TestPowerNeededKw:
    def test_basic(self) -> None:
        # 0.5 kWh w 0.5 h = 1 kW
        assert power_needed_kw(0.5, 1800) == pytest.approx(1.0)
        assert power_needed_kw(-0.5, 1800) == pytest.approx(1.0)

    def test_small_time(self) -> None:
        # 0.01 kWh w 60 s = 0.6 kW
        assert power_needed_kw(0.01, 60) == pytest.approx(0.6)

    def test_below_threshold(self) -> None:
        # 0.005 kWh w 60 s = 0.3 kW
        assert power_needed_kw(0.005, 60) == pytest.approx(0.3)


class TestTolerancePct:
    def test_full_hour(self) -> None:
        assert tolerance_pct(3600, 15, 2) == pytest.approx(15)

    def test_zero_time(self) -> None:
        assert tolerance_pct(0, 15, 2) == pytest.approx(2)

    def test_mid_hour(self) -> None:
        t = tolerance_pct(1800, 15, 2)
        assert 2 < t < 15


class TestComputeIntervention:
    def test_slot_active_can_adjust_when_far(
        self, default_inputs: BalanceInputs
    ) -> None:
        # Make target large enough that oscillation_avoid doesn't block a sign flip.
        default_inputs.remaining_kwh = -2.0
        default_inputs.slot_active = True
        # force a large delta vs current setting so hysteresis does not block
        default_inputs.current_ecoslot_pct = -10
        default_inputs.balancing_slot_time_active = True
        default_inputs.hysteresis_end = 2
        out = compute_intervention(default_inputs)
        assert out.intervene is True

    def test_slot_active_no_adjust_when_close(
        self, default_inputs: BalanceInputs
    ) -> None:
        # target ~ 10% for 0.7kW with 70W/%; if already 10% then hysteresis should block changes
        default_inputs.remaining_kwh = -0.35  # 0.7kW over 1800s
        default_inputs.time_to_end_s = 1800
        default_inputs.slot_active = True
        default_inputs.current_ecoslot_pct = 10
        default_inputs.balancing_slot_time_active = True
        default_inputs.hysteresis_end = 2
        out = compute_intervention(default_inputs)
        assert out.intervene is False
        assert out.reason == "hysteresis"

    def test_other_eco_slot_active_no_intervention(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = -0.5
        default_inputs.time_to_end_s = 1800
        default_inputs.other_eco_slot_active = True
        out = compute_intervention(default_inputs)
        assert out.intervene is False
        assert out.reason == "other_eco_slot_active"

    def test_power_below_threshold_no_intervention(
        self, default_inputs: BalanceInputs
    ) -> None:
        # 0.3 kW needed (0.005 kWh in 60 s)
        default_inputs.remaining_kwh = -0.005
        default_inputs.time_to_end_s = 60
        out = compute_intervention(default_inputs)
        assert out.intervene is False
        assert out.reason == "power_below_threshold"

    def test_power_at_threshold_no_intervention(
        self, default_inputs: BalanceInputs
    ) -> None:
        # exactly 0.6 kW: 0.01 kWh in 60 s
        default_inputs.remaining_kwh = -0.01
        default_inputs.time_to_end_s = 60
        out = compute_intervention(default_inputs)
        assert out.intervene is False

    def test_discharge_intervention(self, default_inputs: BalanceInputs) -> None:
        # remaining < 0 (import) -> need export (+grid); with default pv-cons=500W we need +1000W
        # so battery should discharge about +500W
        default_inputs.remaining_kwh = -0.5
        default_inputs.time_to_end_s = 1800
        out = compute_intervention(default_inputs)
        assert out.intervene is True
        assert out.battery_power_w > 0
        assert out.battery_power_pct > 0
        assert out.battery_power_w == pytest.approx(500.0, abs=80.0)
        assert out.duration_s > 0
        assert out.duration_s <= 1800

    def test_charge_intervention(self, default_inputs: BalanceInputs) -> None:
        # power = 0.31 / (1800/3600) = 0.62 kW > 0.6
        default_inputs.remaining_kwh = 0.31
        default_inputs.time_to_end_s = 1800
        out = compute_intervention(default_inputs)
        assert out.intervene is True
        assert out.battery_power_w < 0
        assert out.battery_power_pct < 0

    def test_hysteresis_no_intervention(self, default_inputs: BalanceInputs) -> None:
        # power = 0.26/(1500/3600) ≈ 0.624 kW > 0.6 so we reach hysteresis check
        default_inputs.remaining_kwh = -0.26
        default_inputs.time_to_end_s = 1500
        default_inputs.current_ecoslot_pct = 20
        out = compute_intervention(default_inputs)
        if out.intervene:
            assert abs(out.battery_power_pct - 20) > default_inputs.hysteresis_end
        else:
            assert out.reason == "hysteresis" or out.reason == "ok"

    def test_discharge_cap_by_inverter(self, default_inputs: BalanceInputs) -> None:
        default_inputs.remaining_kwh = -2.0
        default_inputs.time_to_end_s = 3600
        default_inputs.pv_w = 6000
        default_inputs.consumption_w = 7500  # net import 1.5kW before battery
        default_inputs.p_inverter_w = 8000
        out = compute_intervention(default_inputs)
        assert out.intervene is True
        assert out.battery_power_w <= 2000

    def test_high_pv_reduce_charge_instead_of_discharge(self) -> None:
        """Reprodukcja nieliniowości: przy dużym PV i małym domu wystarczy zmniejszyć ładowanie.

        remaining=-0.06kWh i 660s → ~0.327kW eksportu potrzebne.
        pv=3120W, dom=642W → pv-dom=2478W; aby eksportować ~327W, bateria powinna ładować ~2151W.
        """
        inp = BalanceInputs(
            remaining_kwh=-0.06,
            time_to_end_s=660,
            pv_w=3120,
            consumption_w=642,
            grid_w=0.0,
            soc_pct=33.0,
            p_inverter_w=8200.0,
            p_battery_w=5000.0,
            current_ecoslot_pct=None,
            slot_active=False,
            hysteresis_start=15.0,
            hysteresis_end=2.0,
            balance_threshold_kw=0.3,
            watts_per_percent=70.0,
            balancing_slot_time_active=False,
            other_eco_slot_active=False,
        )
        out = compute_intervention(inp)
        assert out.intervene is True
        assert out.battery_power_w < 0
        assert out.battery_power_w == pytest.approx(-2151.0, abs=120.0)

    def test_duration_not_overshoot(self, default_inputs: BalanceInputs) -> None:
        default_inputs.remaining_kwh = -0.1
        default_inputs.time_to_end_s = 600
        default_inputs.pv_w = 3000
        out = compute_intervention(default_inputs)
        assert out.intervene is True
        assert out.duration_s <= 600

    def test_oscillation_avoid_same_side(self, default_inputs: BalanceInputs) -> None:
        default_inputs.remaining_kwh = 0.001
        default_inputs.time_to_end_s = 600
        default_inputs.current_ecoslot_pct = -10
        default_inputs.hysteresis_end = 5
        out = compute_intervention(default_inputs)
        if not out.intervene and out.reason == "oscillation_avoid":
            assert out.battery_power_pct == -10

    def test_no_oscillation_when_outside_slot_time_window(
        self, default_inputs: BalanceInputs
    ) -> None:
        """balancing_slot_time_active=False (np. on_off=0 lub poza oknem): stary % nie blokuje interwencji."""
        default_inputs.remaining_kwh = -0.5
        default_inputs.time_to_end_s = 1800
        default_inputs.current_ecoslot_pct = -10
        default_inputs.balancing_slot_time_active = False
        out = compute_intervention(default_inputs)
        assert out.intervene is True
        assert out.reason == "ok"
        assert out.battery_power_w > 0


class TestWatchdogPolicy:
    def test_early_window_no_intervention(self, default_inputs: BalanceInputs) -> None:
        default_inputs.remaining_kwh = -0.5
        default_inputs.time_to_end_s = 2400  # 40 min left
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(late_window_s=600, export_buffer_build_minutes=0)
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is False
        assert d.reason == "early_window_no_intervention"

    def test_export_buffer_build_when_pv_surplus_early(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = 0.02
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 3000.0
        default_inputs.consumption_w = 1500.0
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(
            late_window_s=600,
            export_buffer_build_minutes=15,
            export_buffer_target_kwh=0.1,
            export_buffer_discharge_pct=1,
        )
        d = decide_watchdog(
            default_inputs,
            now_s=1000.0,
            state=state,
            cfg=cfg,
            minute_of_hour=3,
        )
        assert d.write_slot is True
        assert d.power_pct == 1
        assert d.mode == "discharge"
        assert d.reason == "export_buffer_build"

    def test_export_buffer_skips_when_pv_not_above_consumption(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = 0.02
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 1000.0
        default_inputs.consumption_w = 2000.0
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(late_window_s=600, export_buffer_build_minutes=15)
        d = decide_watchdog(
            default_inputs,
            now_s=1000.0,
            state=state,
            cfg=cfg,
            minute_of_hour=3,
        )
        assert d.write_slot is False
        assert d.reason == "early_window_no_intervention"

    def test_export_buffer_skips_when_target_reached(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = 0.12
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 3000.0
        default_inputs.consumption_w = 1000.0
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(late_window_s=600, export_buffer_build_minutes=15)
        d = decide_watchdog(
            default_inputs,
            now_s=1000.0,
            state=state,
            cfg=cfg,
            minute_of_hour=3,
        )
        assert d.write_slot is False
        assert d.reason == "early_window_no_intervention"

    def test_export_buffer_skips_after_build_minutes(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = 0.02
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 3000.0
        default_inputs.consumption_w = 1000.0
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(late_window_s=600, export_buffer_build_minutes=15)
        d = decide_watchdog(
            default_inputs,
            now_s=1000.0,
            state=state,
            cfg=cfg,
            minute_of_hour=16,
        )
        assert d.write_slot is False
        assert d.reason == "early_window_no_intervention"

    def test_export_buffer_skips_low_soc(self, default_inputs: BalanceInputs) -> None:
        """SoC ≤ próg: bez rozładowania pod bufor (obrona SOC wyłączona przez release >> remaining)."""
        default_inputs.remaining_kwh = 0.02
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 3000.0
        default_inputs.consumption_w = 1000.0
        default_inputs.soc_pct = 20.0
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(
            late_window_s=600,
            export_buffer_build_minutes=15,
            soc_low_threshold_pct=22.0,
            soc_low_defense_release_remaining_kwh=999.0,
        )
        d = decide_watchdog(
            default_inputs,
            now_s=1000.0,
            state=state,
            cfg=cfg,
            minute_of_hour=3,
        )
        assert d.write_slot is False
        assert d.reason == "early_window_no_intervention"

    def test_export_buffer_skips_when_soc_full_band(
        self, default_inputs: BalanceInputs
    ) -> None:
        """Przy SOC w paśmie obrony pełnej bufor nie włącza +% (nawet gdy hold SOC już nie trzyma)."""
        default_inputs.remaining_kwh = -0.01
        default_inputs.time_to_end_s = 2400
        default_inputs.pv_w = 3000.0
        default_inputs.consumption_w = 1000.0
        default_inputs.soc_pct = 99.6
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(
            late_window_s=600,
            export_buffer_build_minutes=15,
            soc_full_threshold_pct=99.5,
            soc_full_defense_early_release_kwh=0.0,
        )
        d = decide_watchdog(
            default_inputs,
            now_s=1000.0,
            state=state,
            cfg=cfg,
            minute_of_hour=3,
        )
        assert d.write_slot is False
        assert d.reason == "early_window_no_intervention"

    def test_late_window_intervention_when_needed(
        self, default_inputs: BalanceInputs
    ) -> None:
        # 0.06kWh in 300s => 0.72kW required, above late threshold
        default_inputs.remaining_kwh = -0.06
        default_inputs.time_to_end_s = 300
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(
            late_window_s=600, late_power_threshold_kw=0.45, grid_export_bias_w=150.0
        )
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is True
        assert d.enabled is True
        assert d.reason == "ok"

    def test_emergency_import_triggers_even_early(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = -0.01
        default_inputs.time_to_end_s = 2400  # early
        default_inputs.grid_w = -800  # import
        default_inputs.pv_w = 500
        default_inputs.consumption_w = 3000
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=3)
        cfg = WatchdogConfig(late_window_s=600, import_streak_min=3)
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is True

    def test_emergency_unrecoverable_triggers_even_early(
        self, default_inputs: BalanceInputs
    ) -> None:
        # Jeśli w late window (np. 10 min) nie da się już odrobić energii mocą baterii, trzeba zacząć wcześniej.
        # P_battery=5kW -> Emax_late = 5kW * (600s/3600) = 0.833kWh; przy fraction=0.9 daje ~0.75kWh.
        default_inputs.remaining_kwh = -0.8
        default_inputs.time_to_end_s = 2400  # early
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(late_window_s=600, unrecoverable_fraction=0.9)
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is True

    def test_direction_guard_no_charge_when_remaining_negative(
        self, default_inputs: BalanceInputs
    ) -> None:
        # Duże PV i mały dom mogą matematycznie sugerować charge, ale przy remaining<0
        # polityka watchdog nie może aktywnie ładować — zamiast 0% (słabe na GoodWe) minimalne +1% rozładowania.
        default_inputs.remaining_kwh = -0.06
        default_inputs.time_to_end_s = 300  # late window
        default_inputs.pv_w = 5000
        default_inputs.consumption_w = 800
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(
            late_window_s=600, late_power_threshold_kw=0.45, grid_export_bias_w=150.0
        )
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is True
        assert d.power_pct == 1
        assert d.mode == "discharge"
        assert d.reason == "min_discharge_export_assist"

    def test_no_charge_while_hour_surplus_below_buffer(
        self, default_inputs: BalanceInputs
    ) -> None:
        """Mała nadwyżka eksportu (< charge_min_remaining): nie ładuj mimo matematyki charge."""
        default_inputs.remaining_kwh = 0.03
        default_inputs.time_to_end_s = 300
        default_inputs.pv_w = 5000
        default_inputs.consumption_w = 800
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=3)
        cfg = WatchdogConfig(
            late_window_s=600,
            late_power_threshold_kw=0.45,
            grid_export_bias_w=150.0,
            import_streak_min=3,
            charge_min_remaining_kwh=0.05,
        )
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is False
        assert d.reason == "direction_guard_neutral"

    def test_direction_guard_neutral_when_export_assist_disabled(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.remaining_kwh = -0.06
        default_inputs.time_to_end_s = 300
        default_inputs.pv_w = 5000
        default_inputs.consumption_w = 800
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(
            late_window_s=600,
            late_power_threshold_kw=0.45,
            grid_export_bias_w=150.0,
            min_discharge_assist_pct=0,
        )
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is False
        assert d.reason == "direction_guard_neutral"

    def test_soc_full_defense_holds_charge_early_while_net_export(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 2400  # early
        default_inputs.remaining_kwh = 0.05
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(
            late_window_s=600,
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_early_release_kwh=0.0,
            soc_full_defense_late_release_kwh=0.1,
        )
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is True
        assert d.power_pct == -1
        assert d.reason == "soc_full_defense_hold"

    def test_soc_full_defense_releases_at_or_below_zero_early(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 2400  # early
        default_inputs.remaining_kwh = -0.01
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(
            late_window_s=600,
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_early_release_kwh=0.0,
            soc_full_defense_late_release_kwh=0.1,
        )
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.reason != "soc_full_defense_hold"

    def test_soc_full_defense_hour_start_without_carryover_flag(
        self, default_inputs: BalanceInputs
    ) -> None:
        """W :00 remaining=0 bez flagi — nadal carryover (reset bazy godzinowej ≠ puść rozładowanie)."""
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 3500
        default_inputs.remaining_kwh = 0.0
        state = WatchdogState(
            mode="neutral",
            mode_since_s=None,
            import_streak=0,
            soc_full_defense_carryover=False,
        )
        cfg = WatchdogConfig(
            late_window_s=600,
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_early_release_kwh=0.0,
            soc_full_defense_late_release_kwh=0.1,
            soc_full_defense_carryover_minutes=5,
        )
        d = decide_watchdog(
            default_inputs,
            now_s=1000.0,
            state=state,
            cfg=cfg,
            minute_of_hour=0,
        )
        assert d.write_slot is True
        assert d.reason == "soc_full_defense_carryover"

    def test_soc_full_defense_carryover_first_minutes_after_hour_reset(
        self, default_inputs: BalanceInputs
    ) -> None:
        """Po :00 remaining=0 — tarcza z końcówki poprzedniej godziny (carryover w stanie)."""
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 3500  # wcześnie w godzinie
        default_inputs.remaining_kwh = 0.0
        state = WatchdogState(
            mode="neutral",
            mode_since_s=None,
            import_streak=0,
            soc_full_defense_carryover=True,
        )
        cfg = WatchdogConfig(
            late_window_s=600,
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_early_release_kwh=0.0,
            soc_full_defense_late_release_kwh=0.1,
            soc_full_defense_carryover_minutes=5,
        )
        d = decide_watchdog(
            default_inputs,
            now_s=1000.0,
            state=state,
            cfg=cfg,
            minute_of_hour=2,
        )
        assert d.write_slot is True
        assert d.reason == "soc_full_defense_carryover"

    def test_soc_full_defense_carryover_releases_on_net_import(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 3500
        default_inputs.remaining_kwh = -0.01
        state = WatchdogState(
            mode="neutral",
            mode_since_s=None,
            import_streak=0,
            soc_full_defense_carryover=True,
        )
        cfg = WatchdogConfig(
            late_window_s=600,
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_early_release_kwh=0.0,
            soc_full_defense_late_release_kwh=0.1,
        )
        d = decide_watchdog(
            default_inputs,
            now_s=1000.0,
            state=state,
            cfg=cfg,
            minute_of_hour=2,
        )
        assert d.reason != "soc_full_defense_carryover"

    def test_soc_full_defense_last_minute_of_hour_uses_early_release(
        self, default_inputs: BalanceInputs
    ) -> None:
        """W ostatniej minucie (late) nie używaj luźniejszego late, żeby w :59 nadal był hold przy r≈0."""
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 45.0
        default_inputs.remaining_kwh = 0.0
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(
            late_window_s=600,
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_early_release_kwh=-0.3,
            soc_full_defense_late_release_kwh=0.1,
        )
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is True
        assert d.reason == "soc_full_defense_hold"

    def test_soc_full_defense_early_tolerates_import_if_release_negative(
        self, default_inputs: BalanceInputs
    ) -> None:
        """SOC_FULL_DEFENSE_EARLY_RELEASE_KWH<0 = stary „budżet” importu zanim puścisz obronę."""
        default_inputs.soc_pct = 100.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = -0.10
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(
            late_window_s=600,
            soc_full_threshold_pct=99.5,
            soc_full_defense_charge_pct=-1,
            soc_full_defense_early_release_kwh=-0.3,
            soc_full_defense_late_release_kwh=0.1,
        )
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.reason == "soc_full_defense_hold"

    def test_soc_low_defense_holds_while_remaining_above_hour_target(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 18.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = 0.05  # lekka przewaga eksportu — trzymaj obronę
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(
            late_window_s=600,
            soc_low_threshold_pct=22.0,
            soc_low_defense_charge_pct=-1,
            soc_low_defense_release_remaining_kwh=0.0,
        )
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is True
        assert d.power_pct == -1
        assert d.reason == "soc_low_defense_hold"

    def test_soc_low_defense_releases_when_remaining_at_or_below_hour_target(
        self, default_inputs: BalanceInputs
    ) -> None:
        default_inputs.soc_pct = 18.0
        default_inputs.time_to_end_s = 2400
        default_inputs.remaining_kwh = -0.01
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(
            late_window_s=600,
            soc_low_threshold_pct=22.0,
            soc_low_defense_charge_pct=-1,
            soc_low_defense_release_remaining_kwh=0.0,
        )
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.reason != "soc_low_defense_hold"
