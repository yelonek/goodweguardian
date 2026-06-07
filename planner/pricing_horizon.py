"""Horyzont planowania — godziny z pełnym cennikiem (RCE + import G12)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from energy_pricing import pricing_day_breakdown
from guardian_config import TELEMETRY_TZ

log = logging.getLogger("planner")

HorizonSlot = tuple[str, int]


def local_now_naive() -> datetime:
    return datetime.now(ZoneInfo(TELEMETRY_TZ)).replace(tzinfo=None)


def pricing_available_for_day(local_date: date) -> bool:
    """Pełne 24 h RCE+import — bez wyjątku (np. RCE na jutro jeszcze nieopublikowane)."""
    try:
        pricing_day_breakdown(local_date)
        return True
    except Exception as e:
        log.debug("pricing unavailable for %s: %s", local_date.isoformat(), e)
        return False


def priced_horizon_slots(*, now: datetime | None = None) -> list[HorizonSlot]:
    """
    Sloty od bieżącej godziny (włącznie) do ostatniej godziny z cenami.

    Dziś: od ``now`` do 23:00. Jutro: 0–23 gdy ``pricing_day_breakdown(jutro)`` się udaje
    (bez sztywnego harmonogramu — wykrywane przy każdym przebiegu planera).
    """
    now = now or local_now_naive()
    start = now.replace(minute=0, second=0, microsecond=0)
    slots: list[HorizonSlot] = []

    today = start.date()
    if pricing_available_for_day(today):
        for h in range(start.hour, 24):
            slots.append((today.isoformat(), h))

    tomorrow = today + timedelta(days=1)
    if pricing_available_for_day(tomorrow):
        for h in range(24):
            slots.append((tomorrow.isoformat(), h))

    return slots


def slot_to_local_iso(slot: HorizonSlot) -> str:
    d_iso, h = slot
    return f"{d_iso}T{h:02d}:00:00"
