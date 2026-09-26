"""Adapter GoodWe: nastawa w watach wraca na ten sam procent slotu Eco."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from decision.setpoint import Setpoint, slot_pct_from_watts
from ecoslot_config import set_ecoslot
from guardian_log import log_ecoslot_failure


@dataclass(frozen=True)
class EcoSlotCommand:
    """Polecenie slotu Eco po odwrotnym przeliczeniu nastawy."""

    write_slot: bool
    enabled: bool
    power_pct: int
    slot_soc_pct: int
    duration_s: float
    mode: str
    reason: str


def eco_slot_command(setpoint: Setpoint, watts_per_percent: float) -> EcoSlotCommand:
    """Dzieli waty przez ten sam przelicznik. Całkowity procent wraca bez zmiany."""
    pct = slot_pct_from_watts(setpoint.power_w, watts_per_percent)
    release = setpoint.release
    return EcoSlotCommand(
        write_slot=not release,
        enabled=not release,
        power_pct=0 if release else pct,
        slot_soc_pct=int(setpoint.soc_limit_pct),
        duration_s=float(setpoint.duration_s),
        mode=setpoint.mode,
        reason=setpoint.reason,
    )


async def apply_goodwe_setpoint(
    inverter: Any,
    *,
    command: EcoSlotCommand,
    slot_id: str,
    slot_active: bool,
    slot4_write: str,
    now: datetime,
    days: list[int],
) -> None:
    """Zapis albo zgaszenie slotu balansującego. Hold nie wysyła ramki."""
    if not command.write_slot:
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
                    days=days,
                    enabled=False,
                )
            except Exception as e:
                log_ecoslot_failure(slot_id, e)
        return

    if slot4_write == "hold":
        logging.getLogger("guardian").info(
            "slot_hold slot=%s reason=%s power=%+d soc=%d end=%02d:59",
            slot_id,
            command.reason,
            command.power_pct,
            command.slot_soc_pct,
            now.hour,
        )
        return

    try:
        await set_ecoslot(
            inverter,
            slot_id,
            start_h=now.hour,
            start_m=now.minute,
            end_h=now.hour,
            end_m=59,
            power=command.power_pct,
            days=days,
            soc=command.slot_soc_pct,
            enabled=True,
        )
    except Exception as e:
        log_ecoslot_failure(slot_id, e)
