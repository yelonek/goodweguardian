"""
Logika guardiana: Flappy Bird — bilans godzinowy, obrony SOC, limit inwertera.

Konwencja znaku (ujednolicona z mocą sieci):
  − remaining_kWh = ΔE_export − ΔE_import w bieżącej godzinie [kWh].
  − Dodatnie: przewaga energii oddanej do sieci; ujemne: przewaga pobranej z sieci.
  − moc sieci (grid): dodatnie = do sieci (eksport), ujemne = z sieci (import).
Cel: bilans 0 na koniec godziny. Moc baterii: dodatnia = rozładowanie, ujemna = ładowanie.
"""

from dataclasses import dataclass


@dataclass
class BalanceInputs:
    """Wejścia do decide_watchdog (tylko pola używane przez logikę)."""

    remaining_kwh: float
    time_to_end_s: float
    pv_w: float
    consumption_w: float
    soc_pct: float
    p_inverter_w: float
    p_battery_w: float
    watts_per_percent: float = 70.0
    other_eco_slot_active: bool = False
    low_soc_discharge_target_w: float | None = None


@dataclass
class WatchdogConfig:
    """Polityka Flappy Bird: bufor PV w bilansie, korekta deficytu, soak na koniec godziny."""

    grid_export_bias_w: float = 150.0
    recoverable_fraction: float = 0.9
    min_discharge_assist_pct: int = 1
    flappy_buffer_discharge_pct: int = 1
    soak_target_kwh: float = 0.1
    soak_trigger_kwh: float = 0.2
    end_hour_window_s: int = 600
    end_hour_max_remaining_kwh: float = 0.2
    soc_full_threshold_pct: float = 99.5
    soc_full_defense_charge_pct: int = -1
    soc_full_defense_release_power_kw: float = 0.5
    soc_low_threshold_pct: float = 22.0
    soc_low_defense_charge_pct: int = -1
    soc_low_defense_release_remaining_kwh: float = 0.0
    soc_night_reserve_pct: float = 0.0
    soc_night_reserve_charge_pct: int = -1
    night_reserve_hours: frozenset[int] = frozenset({22, 23, 0, 1, 2, 3, 4, 5})
    soc_full_defense_carryover_minutes: int = 5


@dataclass
class WatchdogDecision:
    """Decyzja watchdog: czy ustawić slot, czy wrócić do neutral."""

    write_slot: bool
    enabled: bool
    power_pct: int
    duration_s: float
    mode: str
    reason: str


def power_needed_kw(remaining_kwh: float, time_to_end_s: float) -> float:
    """Moc potrzebna do zbilansowania [kW] (zawsze >= 0)."""
    if time_to_end_s <= 0:
        return 9999.0
    hours_left = time_to_end_s / 3600.0
    return abs(remaining_kwh) / hours_left


def _discharge_cap_w(p_inverter_w: float, pv_w: float, p_battery_w: float) -> float:
    """Maks. moc rozładowania [W] – ograniczenie inwertera (PV + BATERIA <= P_INVERTER)."""
    headroom = max(0.0, p_inverter_w - pv_w)
    return min(p_battery_w, headroom)


def _max_discharge_kw(p_inverter_w: float, pv_w: float, p_battery_w: float) -> float:
    """Maks. moc rozładowania baterii [kW] przy obecnym PV (limit inwertera)."""
    return _discharge_cap_w(p_inverter_w, pv_w, p_battery_w) / 1000.0


def _max_recoverable_kwh(
    p_inverter_w: float,
    pv_w: float,
    p_battery_w: float,
    time_to_end_s: float,
) -> float:
    """Ile kWh deficytu można nadrobić rozładowaniem do końca godziny."""
    return _max_discharge_kw(p_inverter_w, pv_w, p_battery_w) * (time_to_end_s / 3600.0)


def _slot_duration_s(time_to_end_s: float) -> float:
    return min(time_to_end_s, max(60.0, time_to_end_s))


def _neutral_decision(reason: str) -> WatchdogDecision:
    return WatchdogDecision(
        write_slot=False,
        enabled=False,
        power_pct=0,
        duration_s=0.0,
        mode="neutral",
        reason=reason,
    )


def _battery_pct_from_w(target_battery_w: float, watts_per_percent: float) -> int:
    target_pct = max(
        -100, min(100, int(round(target_battery_w / watts_per_percent)))
    )
    if target_pct == 0 and abs(target_battery_w) >= 0.5 * watts_per_percent:
        target_pct = 1 if target_battery_w > 0 else -1
    return target_pct


def _deficit_recovery_decision(
    inp: BalanceInputs,
    cfg: WatchdogConfig,
) -> WatchdogDecision:
    """Natychmiastowa korekta ujemnego bilansu z limitem inwertera (PV + bateria ≤ P_INVERTER)."""
    power_kw = power_needed_kw(inp.remaining_kwh, inp.time_to_end_s)
    max_discharge_kw = _max_discharge_kw(inp.p_inverter_w, inp.pv_w, inp.p_battery_w)
    max_recoverable = _max_recoverable_kwh(
        inp.p_inverter_w, inp.pv_w, inp.p_battery_w, inp.time_to_end_s
    )
    cap_w = _discharge_cap_w(inp.p_inverter_w, inp.pv_w, inp.p_battery_w)
    min_assist = max(0, min(100, int(cfg.min_discharge_assist_pct)))

    need_max_cap = max_discharge_kw > 0.0 and (
        power_kw > max_discharge_kw * 0.95
        or abs(inp.remaining_kwh)
        > max_recoverable * float(cfg.recoverable_fraction)
    )

    if need_max_cap:
        target_battery_w = cap_w
        reason = "deficit_max_cap"
    else:
        required_w = cfg.grid_export_bias_w + power_kw * 1000.0
        target_battery_w = required_w - (inp.pv_w - inp.consumption_w)
        target_battery_w = max(0.0, min(inp.p_battery_w, target_battery_w))
        target_battery_w = min(target_battery_w, cap_w)
        reason = "deficit_recovery"

    target_pct = _battery_pct_from_w(target_battery_w, inp.watts_per_percent)
    if target_pct <= 0 and min_assist > 0:
        target_pct = min_assist
        if reason == "deficit_recovery":
            reason = "deficit_min_assist"
    elif target_pct <= 0:
        return _neutral_decision("deficit_no_headroom")

    return WatchdogDecision(
        write_slot=True,
        enabled=True,
        power_pct=target_pct,
        duration_s=_slot_duration_s(inp.time_to_end_s),
        mode="discharge",
        reason=reason,
    )


def _soak_charge_decision(
    inp: BalanceInputs, cfg: WatchdogConfig, *, target_kwh: float, reason: str
) -> WatchdogDecision:
    """CHARGE dociągający bilans godziny w dół do ``target_kwh`` (PV + ew. import do baterii)."""
    excess_kwh = float(inp.remaining_kwh) - float(target_kwh)
    power_kw = power_needed_kw(excess_kwh, inp.time_to_end_s)
    target_grid_w = -power_kw * 1000.0
    target_battery_w = target_grid_w - (inp.pv_w - inp.consumption_w)
    target_battery_w = max(-inp.p_battery_w, min(inp.p_battery_w, target_battery_w))
    if target_battery_w >= 0.0:
        target_battery_w = -float(inp.watts_per_percent)
    target_pct = _battery_pct_from_w(target_battery_w, inp.watts_per_percent)
    if target_pct >= 0:
        target_pct = -1

    return WatchdogDecision(
        write_slot=True,
        enabled=True,
        power_pct=target_pct,
        duration_s=_slot_duration_s(inp.time_to_end_s),
        mode="charge",
        reason=reason,
    )


def _end_hour_soak_decision(
    inp: BalanceInputs, cfg: WatchdogConfig
) -> WatchdogDecision | None:
    """Koniec godziny: w oknie i nadwyżka eksportu > max → CHARGE do ``end_hour_max_remaining_kwh``."""
    if inp.time_to_end_s > float(cfg.end_hour_window_s):
        return None
    max_rem = float(cfg.end_hour_max_remaining_kwh)
    if float(inp.remaining_kwh) <= max_rem:
        return None
    return _soak_charge_decision(
        inp, cfg, target_kwh=max_rem, reason="end_hour_battery_soak"
    )


def _continuous_soak_decision(
    inp: BalanceInputs, cfg: WatchdogConfig
) -> WatchdogDecision | None:
    """Soak ciągły: przy nadwyżce PV i bilansie > trigger → CHARGE do celu (deadband [target, trigger])."""
    if float(inp.pv_w) <= float(inp.consumption_w):
        return None
    if float(inp.remaining_kwh) <= float(cfg.soak_trigger_kwh):
        return None
    return _soak_charge_decision(
        inp, cfg, target_kwh=float(cfg.soak_target_kwh), reason="continuous_battery_soak"
    )


def decide_watchdog(
    inp: BalanceInputs,
    *,
    cfg: WatchdogConfig,
    minute_of_hour: int | None = None,
    hour_of_day: int | None = None,
    soc_full_defense_carryover: bool = False,
) -> WatchdogDecision:
    """Flappy Bird: bufor PV, korekta deficytu, soak na koniec godziny; obrony SOC bez zmian."""

    low_soc_discharge_cap_active = (
        float(inp.soc_pct) <= float(cfg.soc_low_threshold_pct)
        and inp.low_soc_discharge_target_w is not None
        and float(inp.low_soc_discharge_target_w) > 0.0
    )

    if inp.other_eco_slot_active and not low_soc_discharge_cap_active:
        return WatchdogDecision(
            write_slot=False,
            enabled=False,
            power_pct=0,
            duration_s=0.0,
            mode="neutral",
            reason="other_eco_slot_active",
        )

    if (
        hour_of_day is not None
        and int(hour_of_day) in cfg.night_reserve_hours
        and float(cfg.soc_night_reserve_pct) > 0.0
        and float(inp.soc_pct) <= float(cfg.soc_night_reserve_pct)
    ):
        duration_s = min(inp.time_to_end_s, max(60.0, inp.time_to_end_s))
        return WatchdogDecision(
            write_slot=True,
            enabled=True,
            power_pct=int(cfg.soc_night_reserve_charge_pct),
            duration_s=duration_s,
            mode="charge",
            reason="night_soc_reserve_hold",
        )

    power_kw = power_needed_kw(inp.remaining_kwh, inp.time_to_end_s)

    carryover_min = max(1, int(cfg.soc_full_defense_carryover_minutes))
    carryover_window_s = float(carryover_min * 60)
    in_soc_full_carryover_window = (
        minute_of_hour is not None and int(minute_of_hour) < carryover_min
    )

    if float(inp.soc_pct) >= float(cfg.soc_full_threshold_pct):
        r = float(inp.remaining_kwh)
        release_p = float(cfg.soc_full_defense_release_power_kw)
        if (
            in_soc_full_carryover_window
            and r >= 0.0
            and soc_full_defense_carryover
        ):
            duration_s = min(inp.time_to_end_s, max(60.0, carryover_window_s))
            return WatchdogDecision(
                write_slot=True,
                enabled=True,
                power_pct=int(cfg.soc_full_defense_charge_pct),
                duration_s=duration_s,
                mode="charge",
                reason="soc_full_defense_carryover",
            )
        if (
            r >= 0.0
            or (r < 0.0 and power_kw < release_p)
            or float(inp.time_to_end_s) <= 60.0
        ):
            duration_s = min(inp.time_to_end_s, max(60.0, inp.time_to_end_s))
            return WatchdogDecision(
                write_slot=True,
                enabled=True,
                power_pct=int(cfg.soc_full_defense_charge_pct),
                duration_s=duration_s,
                mode="charge",
                reason="soc_full_defense_hold",
            )

    if float(inp.soc_pct) <= float(cfg.soc_low_threshold_pct):
        low_soc_target_w = inp.low_soc_discharge_target_w
        if low_soc_discharge_cap_active:
            load_deficit_w = float(inp.consumption_w) - float(inp.pv_w)
            if load_deficit_w <= 0.0:
                if float(inp.remaining_kwh) < 0.0:
                    target_pct = max(1, int(cfg.min_discharge_assist_pct))
                    duration_s = min(inp.time_to_end_s, max(60.0, inp.time_to_end_s))
                    return WatchdogDecision(
                        write_slot=True,
                        enabled=True,
                        power_pct=target_pct,
                        duration_s=duration_s,
                        mode="discharge",
                        reason="soc_low_pv_surplus_balance_priority",
                    )
                return WatchdogDecision(
                    write_slot=False,
                    enabled=False,
                    power_pct=0,
                    duration_s=0.0,
                    mode="neutral",
                    reason="soc_low_pv_surplus_no_discharge",
                )
            target_w = min(float(low_soc_target_w), load_deficit_w, float(inp.p_battery_w))
            target_pct = max(
                1,
                min(100, int(round(target_w / float(inp.watts_per_percent)))),
            )
            duration_s = min(inp.time_to_end_s, max(60.0, inp.time_to_end_s))
            return WatchdogDecision(
                write_slot=True,
                enabled=True,
                power_pct=target_pct,
                duration_s=duration_s,
                mode="discharge",
                reason="soc_low_discharge_cap",
            )
        release_kwh = float(cfg.soc_low_defense_release_remaining_kwh)
        if float(inp.remaining_kwh) > release_kwh:
            duration_s = min(inp.time_to_end_s, max(60.0, inp.time_to_end_s))
            return WatchdogDecision(
                write_slot=True,
                enabled=True,
                power_pct=int(cfg.soc_low_defense_charge_pct),
                duration_s=duration_s,
                mode="charge",
                reason="soc_low_defense_hold",
            )

    if float(inp.remaining_kwh) < 0.0:
        return _deficit_recovery_decision(inp, cfg)

    soak = _end_hour_soak_decision(inp, cfg)
    if soak is not None:
        return soak

    cont = _continuous_soak_decision(inp, cfg)
    if cont is not None:
        return cont

    target = float(cfg.soak_target_kwh)
    if (
        float(inp.pv_w) > float(inp.consumption_w)
        and float(inp.remaining_kwh) < target
    ):
        pct = max(1, int(cfg.flappy_buffer_discharge_pct))
        return WatchdogDecision(
            write_slot=True,
            enabled=True,
            power_pct=pct,
            duration_s=_slot_duration_s(inp.time_to_end_s),
            mode="discharge",
            reason="flappy_buffer_build",
        )

    if float(inp.remaining_kwh) >= target:
        return _neutral_decision("flappy_buffer_hold")

    return _neutral_decision("flappy_neutral")
