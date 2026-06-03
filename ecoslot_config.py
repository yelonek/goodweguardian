"""Ustawianie ecoslotów (eco_mode_1..4) z użyciem eco_encoder i goodwe write_setting."""
from types import SimpleNamespace
from typing import Any

from goodwe.sensor import ScheduleType

from eco_encoder import encode_day_of_week, encode_eco_v1, encode_months, encode_schedule


ECO_SETTING_IDS = ("eco_mode_1", "eco_mode_2", "eco_mode_3", "eco_mode_4")


def _slot_12(
    start_h: int,
    start_m: int,
    end_h: int,
    end_m: int,
    power: int,
    days: str | list[int],
    soc: int = 100,
    months: str | list[int] | None = None,
    enabled: bool = True,
) -> bytes:
    """Build 12-byte schedule for ECO_MODE. power: negative=charge, positive=discharge %."""
    on_off = -1 - ScheduleType.ECO_MODE.value if enabled else 0
    day_bits = encode_day_of_week(days) if isinstance(days, (str, list)) else days
    month_bits = encode_months(months) if months is not None else 0
    slot = SimpleNamespace(
        start_h=start_h,
        start_m=start_m,
        end_h=end_h,
        end_m=end_m,
        on_off=on_off,
        day_bits=day_bits,
        power=power,
        soc=soc,
        month_bits=month_bits,
    )
    return encode_schedule(slot)


def _slot_8(
    start_h: int,
    start_m: int,
    end_h: int,
    end_m: int,
    power: int,
    days: str | list[int],
    enabled: bool = True,
) -> bytes:
    """Build 8-byte EcoMode V1 slot. power: negative=charge, positive=discharge %."""
    on_off = -1 if enabled else 0
    day_bits = encode_day_of_week(days) if isinstance(days, (str, list)) else days
    return encode_eco_v1(start_h, start_m, end_h, end_m, power, day_bits, on_off)


async def set_ecoslot(
    inverter: Any,
    slot_id: str,
    start_h: int,
    start_m: int,
    end_h: int,
    end_m: int,
    power: int,
    days: str | list[int] = "Mon-Sun",
    soc: int = 100,
    months: str | list[int] | None = None,
    enabled: bool = True,
) -> None:
    """Ustawia jeden ecoslot (eco_mode_1..4).

    power: ujemne = ładowanie %, dodatnie = rozładowanie %.
    days: 'Mon-Sun' | 'Mon,Tue,Wed' | lista 0..6 (Sun=0).
    months: None (cały rok) | 'Jan,Mar' | lista 1..12.
    """
    if not 10 <= soc <= 100:
        raise ValueError("soc musi być 10..100")
    settings_map = {s.id_: s for s in inverter.settings()}
    if slot_id not in settings_map:
        raise ValueError(f"Ten model inwertera nie obsługuje {slot_id}")

    setting = settings_map[slot_id]
    size = getattr(setting, "size_", 12)

    if size == 12:
        raw = _slot_12(
            start_h, start_m, end_h, end_m, power, days, soc, months, enabled
        )
    elif size == 8:
        if months is not None:
            raise ValueError("EcoMode V1 (8 B) nie obsługuje months")
        raw = _slot_8(start_h, start_m, end_h, end_m, power, days, enabled)
    else:
        raise ValueError(f"Nieobsługiwany rozmiar slotu: {size}")

    await inverter.write_setting(slot_id, raw)
