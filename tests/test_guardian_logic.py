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
    def test_slot_active_can_adjust_when_far(self, default_inputs: BalanceInputs) -> None:
        # Make target large enough that oscillation_avoid doesn't block a sign flip.
        default_inputs.remaining_kwh = -2.0
        default_inputs.slot_active = True
        # force a large delta vs current setting so hysteresis does not block
        default_inputs.current_ecoslot_pct = -10
        default_inputs.balancing_slot_time_active = True
        default_inputs.hysteresis_end = 2
        out = compute_intervention(default_inputs)
        assert out.intervene is True

    def test_slot_active_no_adjust_when_close(self, default_inputs: BalanceInputs) -> None:
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
        cfg = WatchdogConfig(late_window_s=600)
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is False
        assert d.reason == "early_window_no_intervention"

    def test_late_window_intervention_when_needed(self, default_inputs: BalanceInputs) -> None:
        # 0.06kWh in 300s => 0.72kW required, above late threshold
        default_inputs.remaining_kwh = -0.06
        default_inputs.time_to_end_s = 300
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(late_window_s=600, late_power_threshold_kw=0.45, grid_export_bias_w=150.0)
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is True
        assert d.enabled is True
        assert d.reason == "ok"

    def test_emergency_import_triggers_even_early(self, default_inputs: BalanceInputs) -> None:
        default_inputs.remaining_kwh = -0.01
        default_inputs.time_to_end_s = 2400  # early
        default_inputs.grid_w = -800  # import
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=3)
        cfg = WatchdogConfig(late_window_s=600, import_streak_min=3)
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is True

    def test_emergency_unrecoverable_triggers_even_early(self, default_inputs: BalanceInputs) -> None:
        # Jeśli w late window (np. 10 min) nie da się już odrobić energii mocą baterii, trzeba zacząć wcześniej.
        # P_battery=5kW -> Emax_late = 5kW * (600s/3600) = 0.833kWh; przy fraction=0.9 daje ~0.75kWh.
        default_inputs.remaining_kwh = -0.8
        default_inputs.time_to_end_s = 2400  # early
        state = WatchdogState(mode="neutral", mode_since_s=None, import_streak=0)
        cfg = WatchdogConfig(late_window_s=600, unrecoverable_fraction=0.9)
        d = decide_watchdog(default_inputs, now_s=1000.0, state=state, cfg=cfg)
        assert d.write_slot is True
