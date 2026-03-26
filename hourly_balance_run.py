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
from time import time

import goodwe

from ecoslot_config import ECO_SETTING_IDS, set_ecoslot
from guardian_config import (
    BALANCE_POWER_THRESHOLD_KW,
    GRID_EXPORT_BIAS_W,
    get_slot_id,
    HYSTERESIS_TOLERANCE_END,
    HYSTERESIS_TOLERANCE_START,
    INVERTER_IP,
    LATE_WINDOW_S,
    P_BATTERY_W,
    P_INVERTER_W,
    WATTS_PER_PERCENT,
    WATCHDOG_DWELL_S,
    WATCHDOG_IMPORT_STREAK_MIN,
    WATCHDOG_IMPORT_W_THRESHOLD,
    WATCHDOG_LATE_POWER_THRESHOLD_KW,
    WATCHDOG_UNRECOVERABLE_FRACTION,
    SOC_FULL_DEFENSE_CHARGE_PCT,
    SOC_FULL_DEFENSE_EARLY_RELEASE_KWH,
    SOC_FULL_DEFENSE_LATE_RELEASE_KWH,
    SOC_FULL_DEFENSE_THRESHOLD_PCT,
    WATCHDOG_MAX_SLOT_MIN,
)
from guardian_logic import (
    BalanceInputs,
    WatchdogConfig,
    WatchdogState,
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
    load_state,
    load_watchdog_state,
    save_state,
    save_watchdog_state,
)
from sensor_mapping import (
    BATTERY_POWER,
    BATTERY_SOC,
    ENERGY_EXPORTED_TOTAL,
    ENERGY_IMPORTED_TOTAL,
    GRID_POWER,
    HOUSE_CONSUMPTION_POWER,
    PV_POWER,
)


def _get_float(data: dict, key: str, default: float = 0.0) -> float:
    v = data.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


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
    now = datetime.now()
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


async def run_one_cycle() -> None:
    """Wykonuje jeden cykl: odczyt → stan → logika → ewentualna interwencja."""
    now = datetime.now()

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
    if state is None:
        # Bez pliku / złej godziny: baza = teraz; zapis wymagany, inaczej każdy cykl znów ma None i Δ=0.
        E_exp_start, E_imp_start = E_exp, E_imp
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

    inp = BalanceInputs(
        remaining_kwh=remaining_kwh,
        time_to_end_s=time_to_end_s,
        pv_w=pv_w,
        consumption_w=consumption_w,
        grid_w=grid_w,
        soc_pct=soc_pct,
        p_inverter_w=P_INVERTER_W,
        p_battery_w=P_BATTERY_W,
        current_ecoslot_pct=current_pct,
        slot_active=slot_active,
        hysteresis_start=HYSTERESIS_TOLERANCE_START,
        hysteresis_end=HYSTERESIS_TOLERANCE_END,
        balance_threshold_kw=BALANCE_POWER_THRESHOLD_KW,
        watts_per_percent=WATTS_PER_PERCENT,
        balancing_slot_time_active=slot_active,
        other_eco_slot_active=other_eco_active,
    )

    # Watchdog state (anti flip-flop + streak importu)
    wd_raw = load_watchdog_state()
    wd_state = WatchdogState(
        mode=str(wd_raw.get("mode") or "neutral"),
        mode_since_s=wd_raw.get("mode_since"),
        import_streak=int(wd_raw.get("import_streak") or 0),
        last_remaining_kwh=wd_raw.get("last_remaining_kwh"),
    )
    # Aktualizacja streak importu (drogi import)
    if grid_w < WATCHDOG_IMPORT_W_THRESHOLD:
        wd_state.import_streak += 1
    else:
        wd_state.import_streak = 0
    wd_state.last_remaining_kwh = remaining_kwh

    wd_cfg = WatchdogConfig(
        late_window_s=int(LATE_WINDOW_S),
        late_power_threshold_kw=float(WATCHDOG_LATE_POWER_THRESHOLD_KW),
        grid_export_bias_w=float(GRID_EXPORT_BIAS_W),
        import_w_threshold=float(WATCHDOG_IMPORT_W_THRESHOLD),
        import_streak_min=int(WATCHDOG_IMPORT_STREAK_MIN),
        dwell_s=int(WATCHDOG_DWELL_S),
        unrecoverable_fraction=float(WATCHDOG_UNRECOVERABLE_FRACTION),
        soc_full_threshold_pct=float(SOC_FULL_DEFENSE_THRESHOLD_PCT),
        soc_full_defense_charge_pct=int(SOC_FULL_DEFENSE_CHARGE_PCT),
        soc_full_defense_early_release_kwh=float(SOC_FULL_DEFENSE_EARLY_RELEASE_KWH),
        soc_full_defense_late_release_kwh=float(SOC_FULL_DEFENSE_LATE_RELEASE_KWH),
    )

    decision = decide_watchdog(inp, now_s=time(), state=wd_state, cfg=wd_cfg)

    power_kw = power_needed_kw(remaining_kwh, time_to_end_s)
    bal_kw = balancing_power_kw_signed(remaining_kwh, time_to_end_s)

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
        reason=decision.reason,
        threshold_kw=BALANCE_POWER_THRESHOLD_KW,
        commanded_enabled=(True if decision.write_slot else False),
        commanded_pct=(decision.power_pct if decision.write_slot else 0),
        commanded_duration_s=(decision.duration_s if decision.write_slot else 0.0),
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
        wd_state.mode = "neutral"
        wd_state.mode_since_s = None
        save_watchdog_state(
            {
                "mode": wd_state.mode,
                "mode_since": wd_state.mode_since_s,
                "import_streak": wd_state.import_streak,
                "last_remaining_kwh": wd_state.last_remaining_kwh,
            }
        )
        return

    # Ustawienie slotu: od teraz do min(end bieżącej godziny, now + duration)
    start_h, start_m = now.hour, now.minute
    end_h = now.hour
    # Bezpieczniej przy nieliniowościach: nie ustawiaj długich okien.
    # Pętla i tak wykonuje się co minutę, więc dłuższe interwencje będą przedłużane kolejnymi cyklami,
    # a krótkie okna ograniczają przestrzelenie gdy realna moc ≠ model.
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

    # Aktualizacja stanu watchdog po skutecznym zapisie (anti flip-flop)
    wd_state.mode = decision.mode
    if decision.mode in ("charge", "discharge"):
        wd_state.mode_since_s = time()
    save_watchdog_state(
        {
            "mode": wd_state.mode,
            "mode_since": wd_state.mode_since_s,
            "import_streak": wd_state.import_streak,
            "last_remaining_kwh": wd_state.last_remaining_kwh,
        }
    )


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
