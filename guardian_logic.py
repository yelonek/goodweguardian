"""
Logika guardiana: Flappy Bird — bilans godzinowy, obrony SOC, limit inwertera.

Konwencja znaku (ujednolicona z mocą sieci):
  − remaining_kWh = ΔE_export − ΔE_import w bieżącej godzinie [kWh].
  − Dodatnie: przewaga energii oddanej do sieci; ujemne: przewaga pobranej z sieci.
  − moc sieci (grid): dodatnie = do sieci (eksport), ujemne = z sieci (import).
Cel domyślny (fallback bez planera): bilans ~0 na koniec godziny (Flappy Bird).
Egzekucja planu: ``guardian_execution`` + tryb ``exec_mode`` (§13 PLANNING_SYSTEM.md).
Moc baterii: dodatnia = rozładowanie, ujemna = ładowanie.
"""

from dataclasses import dataclass

from planner.config import PLANNER_SOC_MIN_PCT
from planner.models import ExecMode

# Obrony SOC a exec_mode (§13 PLANNING_SYSTEM.md)
PLAN_CHARGE_INTENT_EPS_KWH = 0.05
_EXEC_SKIP_SOC_FULL: frozenset[ExecMode] = frozenset({"export_profit"})
_EXEC_SOC_LOW_ACTIVE: frozenset[ExecMode] = frozenset(
    {"export_pv_surplus", "neutral"}
)


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
    soc_night_reserve_enabled: bool = True
    soc_night_reserve_pct: float = 0.0
    soc_night_reserve_charge_pct: int = -1
    night_reserve_hours: frozenset[int] = frozenset({22, 23, 0, 1, 2, 3, 4, 5})
    soc_full_defense_carryover_minutes: int = 5
    discharge_taper_soc_high_pct: float = 20.0
    discharge_taper_soc_low_pct: float = 10.0
    discharge_taper_max_w_high: float = 1000.0
    discharge_taper_max_w_low: float = 70.0


@dataclass
class WatchdogDecision:
    """Decyzja watchdog: czy ustawić slot, czy wrócić do neutral."""

    write_slot: bool
    enabled: bool
    power_pct: int
    duration_s: float
    mode: str
    reason: str
    slot_soc_pct: int = 100


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


def _steady_decision(
    *,
    power_pct: int,
    mode: str,
    reason: str,
    time_to_end_s: float,
    slot_soc_pct: int = 100,
) -> WatchdogDecision:
    return WatchdogDecision(
        write_slot=True,
        enabled=True,
        power_pct=power_pct,
        duration_s=_slot_duration_s(time_to_end_s),
        mode=mode,
        reason=reason,
        slot_soc_pct=slot_soc_pct,
    )


def load_cover_discharge_w(inp: BalanceInputs) -> float:
    """Minimum rozładowania [W] na pokrycie domu — średnia telemetrii lub bieżące consumption."""
    limit = inp.low_soc_discharge_target_w
    if limit is None and float(inp.consumption_w) > 0.0:
        limit = float(inp.consumption_w)
    if limit is None or limit <= 0.0:
        return 0.0
    return float(limit)


def battery_discharge_cap_w(
    inp: BalanceInputs,
    cfg: WatchdogConfig,
    *,
    full_max_w: float | None = None,
) -> float | None:
    """
    Liniowy sufit mocy rozładowania [W] w strefie ``soc_low .. soc_high``.

    Powyżej ``soc_high``: ``None`` (brak limitu SOC). Poniżej ``soc_low``: clamp ``w_low``.
    Bez podbijania do loadu — dom dopełnia sieć/PV.
    """
    soc = float(inp.soc_pct)
    soc_high = float(cfg.discharge_taper_soc_high_pct)
    soc_low = float(cfg.discharge_taper_soc_low_pct)
    w_high = float(cfg.discharge_taper_max_w_high)
    w_low = float(cfg.discharge_taper_max_w_low)

    if soc > soc_high:
        return None

    if soc_low >= soc_high:
        cap = w_low
    elif soc <= soc_low:
        cap = w_low
    else:
        t = (soc - soc_low) / (soc_high - soc_low)
        cap = w_low + t * (w_high - w_low)

    if full_max_w is not None:
        cap = min(cap, float(full_max_w))
    return max(0.0, cap)


def export_profit_low_soc_taper_max_w(
    inp: BalanceInputs,
    *,
    threshold_pct: float,
    full_max_w: float,
    lfp_cap_w: float,
) -> float:
    """
    Kompatybilność wsteczna dla testów — woła ``battery_discharge_cap_w`` z syntetycznym cfg.

    Zwraca 0 gdy SOC powyżej progu (brak taperu).
    """
    cfg = WatchdogConfig(
        discharge_taper_soc_high_pct=float(threshold_pct),
        discharge_taper_soc_low_pct=10.0,
        discharge_taper_max_w_high=float(lfp_cap_w) if lfp_cap_w > 0.0 else float(full_max_w),
        discharge_taper_max_w_low=70.0,
    )
    cap = battery_discharge_cap_w(inp, cfg, full_max_w=full_max_w)
    return 0.0 if cap is None else cap


def compute_export_profit_pace_w(
    inp: BalanceInputs,
    *,
    plan_discharge_pct: int,
    min_discharge_pct: int,
    taper_max_w: float = 0.0,
) -> float:
    """
    Moc rozładowania [W] dla ``export_profit``.

    Powyżej progu niskiego SOC: max (plan, bateria, inwerter).
    Poniżej: ``taper_max_w`` z ``export_profit_low_soc_taper_max_w`` (LFP / pokrycie loadu).
    Podłoga energii = ``soc_floor_pct`` (osobna gałąź w ``_exec_export_profit``).
    """
    cap_w = _discharge_cap_w(inp.p_inverter_w, inp.pv_w, inp.p_battery_w)
    plan_max_w = plan_discharge_pct * inp.watts_per_percent
    target_w = min(cap_w, float(inp.p_battery_w), plan_max_w)

    if taper_max_w > 0.0:
        target_w = min(target_w, taper_max_w)
    else:
        min_w = min_discharge_pct * inp.watts_per_percent
        if target_w > 0.0 and target_w < min_w:
            target_w = min_w
    return max(0.0, target_w)


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

    taper_cap = battery_discharge_cap_w(inp, cfg)
    if taper_cap is not None:
        target_battery_w = min(target_battery_w, taper_cap)

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


def decide_soc_defenses(
    inp: BalanceInputs,
    *,
    cfg: WatchdogConfig,
    minute_of_hour: int | None = None,
    hour_of_day: int | None = None,
    soc_full_defense_carryover: bool = False,
    exec_mode: ExecMode | None = None,
    plan_battery_delta_kwh: float | None = None,
) -> WatchdogDecision | None:
    """
    Obrony SOC / rezerwa nocna — warstwa nadrzędna; ``None`` = brak interwencji.

    ``exec_mode=None`` (fallback bez planera): pełne obrony jak dotychczas.

    Z planem:
    - **Pełna bateria** — wyłączona w ``export_profit`` (celowe rozładowanie).
    - **Niska bateria** — tylko w ``export_pv_surplus``, ``neutral`` (limit loadu).
    - **``export_profit``** — bez ``soc_low_*``; pacing i taper LFP w ``guardian_execution``.

    Przy niskim SOC i nadwyżce PV (``load ≤ PV``), tylko ``export_pv_surplus`` / ``neutral``:
    - ``remaining_kwh ≥ 0`` → ``soc_low_pv_soak`` (CHARGE −1%, PV do baterii);
    - ``remaining_kwh < 0`` → ``soc_low_pv_surplus_balance_priority`` (DISCHARGE +1%
      tylko na korektę ujemnego bilansu godziny — bez ciężkiego rozładowania magazynu).
    """

    low_soc_discharge_cap_active = (
        float(inp.soc_pct) <= float(cfg.soc_low_threshold_pct)
        and inp.low_soc_discharge_target_w is not None
        and float(inp.low_soc_discharge_target_w) > 0.0
    )

    if inp.other_eco_slot_active and not low_soc_discharge_cap_active:
        return _neutral_decision("other_eco_slot_active")

    if (
        cfg.soc_night_reserve_enabled
        and hour_of_day is not None
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

    skip_soc_full = exec_mode is not None and exec_mode in _EXEC_SKIP_SOC_FULL
    apply_soc_low = exec_mode is None or exec_mode in _EXEC_SOC_LOW_ACTIVE

    if not skip_soc_full:
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

    if apply_soc_low and float(inp.soc_pct) <= float(cfg.soc_low_threshold_pct):
        low_soc_target_w = inp.low_soc_discharge_target_w
        at_soc_floor = float(inp.soc_pct) <= float(PLANNER_SOC_MIN_PCT) + 0.5
        plan_wants_charge = (
            plan_battery_delta_kwh is not None
            and float(plan_battery_delta_kwh) > PLAN_CHARGE_INTENT_EPS_KWH
        )
        prefer_charge_over_export = at_soc_floor or plan_wants_charge
        hour_export_surplus = float(inp.remaining_kwh) >= 0.0

        if low_soc_discharge_cap_active:
            load_deficit_w = float(inp.consumption_w) - float(inp.pv_w)
            if load_deficit_w <= 0.0:
                # PV ≥ load: domyślnie PV → bateria; DISCHARGE +1% tylko gdy godzina w deficycie.
                if prefer_charge_over_export or hour_export_surplus:
                    duration_s = min(inp.time_to_end_s, max(60.0, inp.time_to_end_s))
                    return WatchdogDecision(
                        write_slot=True,
                        enabled=True,
                        power_pct=int(cfg.soc_low_defense_charge_pct),
                        duration_s=duration_s,
                        mode="charge",
                        reason="soc_low_pv_soak",
                    )
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
            # Deficyt loadu przy niskim SOC: sieć dopełnia dom gdy godzina na plusie
            # lub plan/SOC wymaga ładowania — nie rozładowuj baterii (LFP / import).
            if prefer_charge_over_export or hour_export_surplus:
                return _neutral_decision("soc_low_grid_covers_load")
            target_w = min(float(low_soc_target_w), load_deficit_w, float(inp.p_battery_w))
            taper_cap = battery_discharge_cap_w(inp, cfg)
            if taper_cap is not None:
                target_w = min(target_w, taper_cap)
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

    return None


def decide_flappy_relative(
    inp: BalanceInputs,
    *,
    cfg: WatchdogConfig,
    target_net_kwh: float,
    early_intervention_kw: float = 1.0,
) -> WatchdogDecision:
    """Flappy Bird względem ``target_net_kwh`` (tryb ``neutral`` planera)."""
    net = float(inp.remaining_kwh)
    target = float(target_net_kwh)
    gap = net - target

    if float(inp.consumption_w) > float(inp.pv_w) and net >= target:
        return _neutral_decision("neutral_wait_above_target")

    if net < target:
        shortfall = target - net
        required_kw = power_needed_kw(shortfall, inp.time_to_end_s)
        pv_surplus_w = float(inp.pv_w) - float(inp.consumption_w)
        if pv_surplus_w > 0.0 and required_kw < early_intervention_kw:
            pct = max(1, int(cfg.flappy_buffer_discharge_pct))
            return _steady_decision(
                power_pct=pct,
                mode="discharge",
                reason="neutral_pv_first",
                time_to_end_s=inp.time_to_end_s,
            )
        deficit_inp = BalanceInputs(
            remaining_kwh=gap,
            time_to_end_s=inp.time_to_end_s,
            pv_w=inp.pv_w,
            consumption_w=inp.consumption_w,
            soc_pct=inp.soc_pct,
            p_inverter_w=inp.p_inverter_w,
            p_battery_w=inp.p_battery_w,
            watts_per_percent=inp.watts_per_percent,
            other_eco_slot_active=inp.other_eco_slot_active,
            low_soc_discharge_target_w=inp.low_soc_discharge_target_w,
        )
        return _deficit_recovery_decision(deficit_inp, cfg)

    # target_net < 0 = planowany import; net > target → brakuje importu — nie soakuj PV do baterii.
    if target < 0.0 and net > target:
        return _neutral_decision("neutral_import_shortfall_hold")

    soak = _end_hour_soak_decision(inp, cfg)
    if soak is not None and net > target:
        return soak

    if (
        target >= 0.0
        and float(inp.pv_w) > float(inp.consumption_w)
        and net > target + float(cfg.soak_trigger_kwh)
    ):
        return _soak_charge_decision(
            inp, cfg, target_kwh=target, reason="neutral_pv_soak"
        )

    if float(inp.pv_w) > float(inp.consumption_w) and net < target + float(cfg.soak_target_kwh):
        pct = max(1, int(cfg.flappy_buffer_discharge_pct))
        return _steady_decision(
            power_pct=pct,
            mode="discharge",
            reason="neutral_buffer_build",
            time_to_end_s=inp.time_to_end_s,
        )

    if net >= target:
        return _neutral_decision("neutral_hold")

    return _neutral_decision("neutral_idle")


def decide_watchdog(
    inp: BalanceInputs,
    *,
    cfg: WatchdogConfig,
    minute_of_hour: int | None = None,
    hour_of_day: int | None = None,
    soc_full_defense_carryover: bool = False,
) -> WatchdogDecision:
    """Fallback bez planera: Flappy ~0 na liczniku."""

    soc = decide_soc_defenses(
        inp,
        cfg=cfg,
        minute_of_hour=minute_of_hour,
        hour_of_day=hour_of_day,
        soc_full_defense_carryover=soc_full_defense_carryover,
    )
    if soc is not None:
        return soc

    if float(inp.remaining_kwh) < 0.0:
        return _deficit_recovery_decision(inp, cfg)

    soak = _end_hour_soak_decision(inp, cfg)
    if soak is not None:
        return soak

    cont = _continuous_soak_decision(inp, cfg)
    if cont is not None:
        return cont

    target = float(cfg.soak_target_kwh)
    in_end_hour_window = inp.time_to_end_s <= float(cfg.end_hour_window_s)
    if (
        not in_end_hour_window
        and float(inp.pv_w) > float(inp.consumption_w)
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
