"""
Guardian: godzinowy balans export/import.

Domyślnie: pętla wykonująca cykl co minutę (wyrównanie do początku następnej minuty lokalnej).
Jeden przebieg: `uv run python hourly_balance_run.py --once` (np. z harmonogramu).
"""
import argparse
import asyncio
import logging
from datetime import datetime

import goodwe

from ecoslot_config import set_ecoslot
from guardian_config import (
    BALANCE_POWER_THRESHOLD_KW,
    get_slot_id,
    HYSTERESIS_TOLERANCE_END,
    HYSTERESIS_TOLERANCE_START,
    INVERTER_IP,
    P_BATTERY_W,
    P_INVERTER_W,
    WATTS_PER_PERCENT,
)
from guardian_logic import BalanceInputs, compute_intervention, power_needed_kw
from guardian_log import (
    balancing_power_kw_signed,
    log_dashboard,
    log_ecoslot_failure,
    log_inputs,
    log_intervention,
    setup_logging,
)
from guardian_state import load_state, save_state
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


def _slot_active(slot: object | None, now: datetime) -> bool:
    """Slot jest aktywny gdy on_off != 0 i bieżący czas w [start, end]."""
    if slot is None:
        return False
    on_off = getattr(slot, "on_off", 0)
    if on_off == 0:
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
    remaining_kwh = delta_imp - delta_exp  # minus = przewaga importu (plan)

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
    )
    out = compute_intervention(inp)
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
        ecoslot_pct=current_pct,
        intervene=out.intervene,
        reason=out.reason,
        threshold_kw=BALANCE_POWER_THRESHOLD_KW,
        target_battery_w=out.battery_power_w if out.intervene else None,
        target_battery_pct=out.battery_power_pct if out.intervene else None,
        duration_s=out.duration_s if out.intervene else None,
    )

    log_intervention(
        now=now,
        remaining_kwh=remaining_kwh,
        power_needed_kw=power_kw,
        intervene=out.intervene,
        battery_power_w=out.battery_power_w if out.intervene else None,
        battery_power_pct=out.battery_power_pct if out.intervene else None,
        duration_s=out.duration_s if out.intervene else None,
        reason=out.reason,
    )

    if not out.intervene:
        return

    # Ustawienie slotu: od teraz do min(end bieżącej godziny, now + duration)
    start_h, start_m = now.hour, now.minute
    end_h = now.hour
    duration_min = out.duration_s / 60.0
    end_m = min(59, start_m + int(round(duration_min)))
    if end_m <= start_m:
        end_m = min(59, start_m + 1)

    days = _days_today_only(now)
    try:
        await set_ecoslot(
            inverter,
            slot_id,
            start_h=start_h,
            start_m=start_m,
            end_h=end_h,
            end_m=end_m,
            power=out.battery_power_pct,
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
