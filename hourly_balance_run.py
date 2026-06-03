"""
Guardian: godzinowy balans export/import.

Domyślnie: pętla wykonująca cykl co minutę (wyrównanie do początku następnej minuty lokalnej).
Jeden przebieg: `uv run python hourly_balance_run.py --once` (np. z harmonogramu).
"""

import argparse
import asyncio
import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import goodwe

from ecoslot_config import ECO_SETTING_IDS, set_ecoslot
from guardian_watchdog_override import effective_watchdog_soc
from guardian_config import (
    BALANCE_POWER_THRESHOLD_KW,
    END_HOUR_MAX_REMAINING_KWH,
    END_HOUR_WINDOW_S,
    FLAPPY_BUFFER_DISCHARGE_PCT,
    get_slot_id,
    GRID_EXPORT_BIAS_W,
    INVERTER_IP,
    P_BATTERY_W,
    P_INVERTER_W,
    RECOVERABLE_FRACTION,
    SOAK_TARGET_KWH,
    SOAK_TRIGGER_KWH,
    SOC_FULL_DEFENSE_CARRYOVER_MINUTES,
    SOC_FULL_DEFENSE_CHARGE_PCT,
    SOC_FULL_DEFENSE_MAX_SLOT_MIN,
    SOC_FULL_DEFENSE_RELEASE_POWER_KW,
    SOC_LOW_DEFENSE_CHARGE_PCT,
    SOC_LOW_DEFENSE_RELEASE_REMAINING_KWH,
    SOC_LOW_DISCHARGE_AVG_MINUTES,
    SOC_LOW_DISCHARGE_FALLBACK_W,
    SOC_LOW_DISCHARGE_MAX_W,
    TELEMETRY_ENABLED,
    TELEMETRY_TZ,
    WATTS_PER_PERCENT,
    WATCHDOG_MAX_SLOT_MIN,
    WATCHDOG_MIN_DISCHARGE_ASSIST_PCT,
)
from guardian_control import effective_control_enabled
from guardian_logic import (
    BalanceInputs,
    WatchdogConfig,
    decide_watchdog,
    power_needed_kw,
)
from guardian_log import (
    balancing_power_kw_signed,
    log_dashboard,
    log_ecoslot_failure,
    log_inputs,
    log_intervention,
    setup_logging,
)
from guardian_state import (
    load_soc_full_defense_carryover,
    load_state,
    save_soc_full_defense_carryover,
    save_state,
)
from sensor_mapping import (
    BATTERY_POWER,
    BATTERY_SOC,
    ENERGY_EXPORTED_DAY,
    ENERGY_EXPORTED_TOTAL,
    ENERGY_IMPORTED_DAY,
    ENERGY_IMPORTED_TOTAL,
    GRID_POWER,
    HOUSE_CONSUMPTION_POWER,
    PV_ENERGY_TOTAL,
    PV_POWER,
)
from telemetry_store import (
    CycleTelemetryRecord,
    append_cycle_record,
    hour_start_counters_from_telemetry,
    build_ts_and_calendar,
    recent_consumption_average_w,
)


def _local_now() -> datetime:
    """Lokalny czas ścienny (TELEMETRY_TZ), naive — sloty i bilans godzinowy."""
    return datetime.now(ZoneInfo(TELEMETRY_TZ)).replace(tzinfo=None)


def _get_float(data: dict, key: str, default: float = 0.0) -> float:
    v = data.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _get_optional_float(data: dict, key: str) -> float | None:
    v = data.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _slot_time_in_window(slot: object | None, now: datetime) -> bool:
    """Czy bieżący czas mieści się w [start, end] slotu (bez sprawdzania on_off)."""
    if slot is None:
        return False
    sh = getattr(slot, "start_h", 0)
    sm = getattr(slot, "start_m", 0)
    eh = getattr(slot, "end_h", 0)
    em = getattr(slot, "end_m", 0)
    h, m = now.hour, now.minute
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    now_min = h * 60 + m
    return start_min <= now_min <= end_min


def _slot_active(slot: object | None, now: datetime) -> bool:
    """Slot jest aktywny gdy on_off != 0 i bieżący czas w [start, end]."""
    if slot is None:
        return False
    on_off = getattr(slot, "on_off", 0)
    if on_off == 0:
        return False
    return _slot_time_in_window(slot, now)


def _current_ecoslot_pct(slot: object | None) -> int | None:
    if slot is None:
        return None
    power = getattr(slot, "power", None)
    if power is None:
        get_power = getattr(slot, "get_power", None)
        if callable(get_power):
            return get_power()
        return None
    return int(power)


def _days_today_only(now: datetime) -> list[int]:
    """GoodWe: 0=Sun, 1=Mon, ..., 6=Sat. Python weekday(): Mon=0, Sun=6."""
    # python weekday 0=Mon -> goodwe 1; 6=Sun -> goodwe 0
    w = now.weekday()
    return [(w + 1) % 7]


def _seconds_until_next_minute_start() -> float:
    """Sekundy do początku następnej minuty (krótki margines po :00)."""
    now = _local_now()
    elapsed = now.second + now.microsecond / 1_000_000
    return max(0.05, 60.0 - elapsed + 0.05)


async def _any_other_eco_slot_active(
    inverter: object, skip_slot_id: str, now: datetime
) -> bool:
    """True, gdy któryś z eco_mode_1..4 (oprócz slotu balansującego) ma on_off i czas w oknie."""
    log = logging.getLogger("guardian")
    for sid in ECO_SETTING_IDS:
        if sid == skip_slot_id:
            continue
        try:
            s = await inverter.read_setting(sid)
            if _slot_active(s, now):
                return True
        except Exception as e:
            log.debug("read_setting %s (other eco): %s", sid, e)
    return False


def _next_soc_full_carryover_flag(
    current: bool,
    *,
    decision_reason: str,
    now_minute: int,
    time_to_end_s: float,
    soc_pct: float,
    soc_full_threshold_pct: float,
    carryover_minutes: int,
) -> bool:
    """Ustawia flagę carryover: aktywna tarcza w ostatnich N min poprzedniej godziny."""
    carryover_min = max(1, int(carryover_minutes))
    last_n_min_of_hour = time_to_end_s <= float(carryover_min * 60)
    if decision_reason == "soc_full_defense_hold" and last_n_min_of_hour:
        return True
    if now_minute >= carryover_min or soc_pct < soc_full_threshold_pct:
        return False
    if (
        current
        and now_minute < carryover_min
        and decision_reason
        not in ("soc_full_defense_hold", "soc_full_defense_carryover")
        and decision_reason != "other_eco_slot_active"
    ):
        return False
    return current


async def run_one_cycle() -> None:
    """Wykonuje jeden cykl: odczyt → stan → logika → ewentualna interwencja."""
    now = _local_now()

    if not INVERTER_IP:
        logging.getLogger("guardian").error("INVERTER_IP nie ustawione")
        return

    inverter = await goodwe.connect(INVERTER_IP)
    runtime = await inverter.read_runtime_data()

    E_exp = _get_float(runtime, ENERGY_EXPORTED_TOTAL)
    E_imp = _get_float(runtime, ENERGY_IMPORTED_TOTAL)
    pv_w = _get_float(runtime, PV_POWER)
    grid_w = _get_float(runtime, GRID_POWER)
    consumption_w = _get_float(runtime, HOUSE_CONSUMPTION_POWER)
    soc_pct = _get_float(runtime, BATTERY_SOC)
    battery_w = _get_float(runtime, BATTERY_POWER)

    # Zapis stanu na :00
    if now.minute == 0:
        save_state(now, E_exp, E_imp)

    state = load_state(now)
    log = logging.getLogger("guardian")
    if state is None:
        recovered = hour_start_counters_from_telemetry(now)
        if recovered is not None:
            E_exp_start, E_imp_start = recovered
            log.info(
                "Baza godziny z telemetrii (start w środku godziny / po migracji): "
                "E_exp=%.3f E_imp=%.3f",
                E_exp_start,
                E_imp_start,
            )
        else:
            E_exp_start, E_imp_start = E_exp, E_imp
            log.warning(
                "Brak stanu i telemetrii dla godziny — baza = bieżące liczniki (bilans od teraz)"
            )
        save_state(now, E_exp_start, E_imp_start)
    else:
        E_exp_start, E_imp_start = state

    delta_imp = E_imp - E_imp_start
    delta_exp = E_exp - E_exp_start
    # Dodatnie = więcej energii ODDANEJ do sieci niż pobrane (Δexp − Δimp), spójnie z grid_w > 0 = eksport.
    remaining_kwh = delta_exp - delta_imp

    time_to_end_s = (59 - now.minute) * 60 + (60 - now.second)
    if now.minute == 59:
        time_to_end_s = min(time_to_end_s, 60)

    log_inputs(
        now=now,
        E_exp=E_exp,
        E_imp=E_imp,
        E_exp_start=E_exp_start,
        E_imp_start=E_imp_start,
        pv_w=pv_w,
        grid_w=grid_w,
        consumption_w=consumption_w,
    )

    slot_id = get_slot_id()
    try:
        current_slot = await inverter.read_setting(slot_id)
    except Exception as e:
        logging.getLogger("guardian").warning("read_setting %s failed: %s", slot_id, e)
        current_slot = None

    slot_active = _slot_active(current_slot, now)
    current_pct = _current_ecoslot_pct(current_slot)
    other_eco_active = await _any_other_eco_slot_active(inverter, slot_id, now)
    ws = effective_watchdog_soc()
    low_soc_discharge_target_w = None
    if soc_pct <= ws.soc_low_defense_threshold_pct:
        avg_window_min = int(SOC_LOW_DISCHARGE_AVG_MINUTES)
        recent_avg_w = recent_consumption_average_w(now, avg_window_min)
        if recent_avg_w is None and float(SOC_LOW_DISCHARGE_FALLBACK_W) > 0.0:
            recent_avg_w = float(SOC_LOW_DISCHARGE_FALLBACK_W)
        if recent_avg_w is not None and recent_avg_w > 0.0:
            max_w = float(SOC_LOW_DISCHARGE_MAX_W)
            low_soc_discharge_target_w = min(recent_avg_w, max_w) if max_w > 0.0 else recent_avg_w

    inp = BalanceInputs(
        remaining_kwh=remaining_kwh,
        time_to_end_s=time_to_end_s,
        pv_w=pv_w,
        consumption_w=consumption_w,
        soc_pct=soc_pct,
        p_inverter_w=P_INVERTER_W,
        p_battery_w=P_BATTERY_W,
        watts_per_percent=WATTS_PER_PERCENT,
        other_eco_slot_active=other_eco_active,
        low_soc_discharge_target_w=low_soc_discharge_target_w,
    )

    wd_cfg = WatchdogConfig(
        grid_export_bias_w=float(GRID_EXPORT_BIAS_W),
        recoverable_fraction=float(RECOVERABLE_FRACTION),
        min_discharge_assist_pct=int(WATCHDOG_MIN_DISCHARGE_ASSIST_PCT),
        flappy_buffer_discharge_pct=int(FLAPPY_BUFFER_DISCHARGE_PCT),
        soak_target_kwh=float(SOAK_TARGET_KWH),
        soak_trigger_kwh=float(SOAK_TRIGGER_KWH),
        end_hour_window_s=int(END_HOUR_WINDOW_S),
        end_hour_max_remaining_kwh=float(END_HOUR_MAX_REMAINING_KWH),
        soc_full_threshold_pct=float(ws.soc_full_defense_threshold_pct),
        soc_full_defense_charge_pct=int(SOC_FULL_DEFENSE_CHARGE_PCT),
        soc_full_defense_release_power_kw=float(SOC_FULL_DEFENSE_RELEASE_POWER_KW),
        soc_low_threshold_pct=float(ws.soc_low_defense_threshold_pct),
        soc_low_defense_charge_pct=int(SOC_LOW_DEFENSE_CHARGE_PCT),
        soc_low_defense_release_remaining_kwh=float(SOC_LOW_DEFENSE_RELEASE_REMAINING_KWH),
        soc_night_reserve_pct=float(ws.soc_night_reserve_pct),
        soc_night_reserve_charge_pct=int(ws.soc_night_reserve_charge_pct),
        night_reserve_hours=ws.night_reserve_hours,
        soc_full_defense_carryover_minutes=max(1, int(SOC_FULL_DEFENSE_CARRYOVER_MINUTES)),
    )

    carryover_min = max(1, int(SOC_FULL_DEFENSE_CARRYOVER_MINUTES))
    soc_full_carryover = load_soc_full_defense_carryover()

    decision = decide_watchdog(
        inp,
        cfg=wd_cfg,
        minute_of_hour=now.minute,
        hour_of_day=now.hour,
        soc_full_defense_carryover=soc_full_carryover,
    )

    soc_full_carryover = _next_soc_full_carryover_flag(
        soc_full_carryover,
        decision_reason=decision.reason,
        now_minute=now.minute,
        time_to_end_s=time_to_end_s,
        soc_pct=soc_pct,
        soc_full_threshold_pct=float(ws.soc_full_defense_threshold_pct),
        carryover_minutes=carryover_min,
    )
    save_soc_full_defense_carryover(soc_full_carryover)

    power_kw = power_needed_kw(remaining_kwh, time_to_end_s)
    bal_kw = balancing_power_kw_signed(remaining_kwh, time_to_end_s)

    control_ok, control_source = effective_control_enabled()
    reason_for_log = (
        f"{decision.reason} [control_off:{control_source}]"
        if not control_ok
        else decision.reason
    )
    cmd_active = control_ok and decision.write_slot

    log_dashboard(
        now=now,
        remaining_kwh=remaining_kwh,
        balancing_kw=bal_kw,
        grid_w=grid_w,
        pv_w=pv_w,
        consumption_w=consumption_w,
        soc_pct=soc_pct,
        battery_w=battery_w,
        time_to_end_s=time_to_end_s,
        delta_imp_kwh=delta_imp,
        delta_exp_kwh=delta_exp,
        slot_active=slot_active,
        other_eco_active=other_eco_active,
        ecoslot_pct=current_pct,
        intervene=decision.write_slot,
        reason=reason_for_log,
        threshold_kw=BALANCE_POWER_THRESHOLD_KW,
        commanded_enabled=cmd_active,
        commanded_pct=(decision.power_pct if cmd_active else 0),
        commanded_duration_s=(decision.duration_s if cmd_active else 0.0),
    )

    log_intervention(
        now=now,
        remaining_kwh=remaining_kwh,
        power_needed_kw=power_kw,
        intervene=decision.write_slot,
        battery_power_w=(decision.power_pct * WATTS_PER_PERCENT)
        if decision.write_slot
        else None,
        battery_power_pct=decision.power_pct if decision.write_slot else None,
        duration_s=decision.duration_s if decision.write_slot else None,
        reason=decision.reason,
    )

    if TELEMETRY_ENABLED:
        ts_utc, loc_date, loc_h, loc_m, loc_wd, loc_we = build_ts_and_calendar(now)
        e_day_imp = _get_optional_float(runtime, ENERGY_IMPORTED_DAY)
        e_day_exp = _get_optional_float(runtime, ENERGY_EXPORTED_DAY)
        E_pv = _get_optional_float(runtime, PV_ENERGY_TOTAL)
        try:
            append_cycle_record(
                CycleTelemetryRecord(
                    ts_utc=ts_utc,
                    local_date=loc_date,
                    local_hour=loc_h,
                    local_minute=loc_m,
                    weekday=loc_wd,
                    is_weekend=loc_we,
                    grid_w=grid_w,
                    pv_w=pv_w,
                    battery_w=battery_w,
                    consumption_w=consumption_w,
                    soc_pct=soc_pct,
                    E_imp_kwh=E_imp,
                    E_exp_kwh=E_exp,
                    E_pv_kwh=E_pv,
                    e_day_imp_kwh=e_day_imp,
                    e_day_exp_kwh=e_day_exp,
                    remaining_kwh=remaining_kwh,
                    time_to_end_s=time_to_end_s,
                    delta_imp_kwh=delta_imp,
                    delta_exp_kwh=delta_exp,
                    slot_balancing_active=slot_active,
                    other_eco_active=other_eco_active,
                    ecoslot_pct=current_pct,
                    watchdog_write_slot=decision.write_slot,
                    watchdog_reason=decision.reason,
                    guardian_control_enabled=control_ok,
                    control_source=control_source,
                    cmd_enabled=cmd_active,
                    cmd_pct=decision.power_pct if cmd_active else 0,
                    cmd_duration_s=float(decision.duration_s if cmd_active else 0.0),
                )
            )
        except Exception:
            logging.getLogger("guardian").warning(
                "telemetry build/append failed", exc_info=True
            )

    if not control_ok:
        return

    if not decision.write_slot:
        # Powrót do normy: jeśli slot balansujący aktywny, wyłącz go, żeby GoodWe działało samodzielnie.
        if slot_active:
            try:
                await set_ecoslot(
                    inverter,
                    slot_id,
                    start_h=now.hour,
                    start_m=now.minute,
                    end_h=now.hour,
                    end_m=min(59, now.minute + 1),
                    power=0,
                    days=_days_today_only(now),
                    enabled=False,
                )
            except Exception as e:
                log_ecoslot_failure(slot_id, e)
        return

    # Ustawienie slotu: od teraz do min(end bieżącej godziny, now + duration)
    start_h, start_m = now.hour, now.minute
    end_h = now.hour
    # Bezpieczniej przy nieliniowościach: nie ustawiaj długich okien.
    # Pętla i tak wykonuje się co minutę, więc dłuższe interwencje będą przedłużane kolejnymi cyklami,
    # a krótkie okna ograniczają przestrzelenie gdy realna moc ≠ model.
    if decision.reason in (
        "soc_full_defense_hold",
        "soc_full_defense_carryover",
        "soc_low_defense_hold",
        "soc_low_discharge_cap",
    ):
        MAX_SLOT_MIN = max(1, int(SOC_FULL_DEFENSE_MAX_SLOT_MIN))
    else:
        MAX_SLOT_MIN = max(1, int(WATCHDOG_MAX_SLOT_MIN))
    duration_min = max(1, int(math.ceil((decision.duration_s or 0.0) / 60.0)))
    duration_min = min(MAX_SLOT_MIN, duration_min)
    end_m = min(59, start_m + duration_min)

    days = _days_today_only(now)
    try:
        await set_ecoslot(
            inverter,
            slot_id,
            start_h=start_h,
            start_m=start_m,
            end_h=end_h,
            end_m=end_m,
            power=decision.power_pct,
            days=days,
            enabled=True,
        )
    except Exception as e:
        log_ecoslot_failure(slot_id, e)


async def run_loop_forever() -> None:
    """Powtarza cykl co minutę (start tuż po pełnej minucie zegara)."""
    log = logging.getLogger("guardian")
    log.info("Guardian: pętla co minutę (Ctrl+C aby zakończyć)")
    while True:
        try:
            await run_one_cycle()
        except Exception:
            log.exception("cykl zakończony błędem – kontynuacja po przerwie")
        await asyncio.sleep(_seconds_until_next_minute_start())


def main() -> None:
    parser = argparse.ArgumentParser(description="Guardian: godzinowy balans energii")
    parser.add_argument(
        "--once",
        action="store_true",
        help="jeden cykl i wyjście (zamiast pętli co minutę)",
    )
    args = parser.parse_args()
    setup_logging()
    log = logging.getLogger("guardian")
    if args.once:
        asyncio.run(run_one_cycle())
    else:
        try:
            asyncio.run(run_loop_forever())
        except KeyboardInterrupt:
            log.info("Guardian: przerwano przez użytkownika")


if __name__ == "__main__":
    main()
