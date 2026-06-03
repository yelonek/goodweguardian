"""Odczyt i zapis ecoslotów (eco_mode_1..4) dla API dashboardu."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import goodwe

from ecoslot_config import ECO_SETTING_IDS, set_ecoslot
from guardian_config import ECO_SLOT_BALANCING, INVERTER_IP, TELEMETRY_TZ

logger = logging.getLogger(__name__)

_ECO_READ_TIMEOUT_S = 15.0


def _local_now() -> datetime:
    return datetime.now(ZoneInfo(TELEMETRY_TZ)).replace(tzinfo=None)


def _slot_time_in_window(slot: object | None, now: datetime) -> bool:
    if slot is None:
        return False
    sh = getattr(slot, "start_h", 0)
    sm = getattr(slot, "start_m", 0)
    eh = getattr(slot, "end_h", 0)
    em = getattr(slot, "end_m", 0)
    now_min = now.hour * 60 + now.minute
    return sh * 60 + sm <= now_min <= eh * 60 + em


def _slot_enabled(slot: object | None) -> bool:
    if slot is None:
        return False
    return getattr(slot, "on_off", 0) != 0


def _slot_power_pct(slot: object | None) -> int | None:
    if slot is None:
        return None
    power = getattr(slot, "power", None)
    if power is None:
        get_power = getattr(slot, "get_power", None)
        if callable(get_power):
            return int(get_power())
        return None
    return int(power)


def balancing_slot_id() -> str:
    return f"eco_mode_{ECO_SLOT_BALANCING}"


def editable_slot_ids() -> tuple[str, ...]:
    skip = balancing_slot_id()
    return tuple(sid for sid in ECO_SETTING_IDS if sid != skip)


def assert_editable_slot(slot_id: str) -> None:
    if slot_id not in editable_slot_ids():
        raise ValueError(
            f"Slot {slot_id} jest zarezerwowany dla Guardiana ({balancing_slot_id()})"
        )


def slot_to_payload(slot: object | None, *, now: datetime) -> dict[str, Any]:
    if slot is None:
        return {
            "present": False,
            "enabled": False,
            "active_now": False,
            "start_h": None,
            "start_m": None,
            "end_h": None,
            "end_m": None,
            "power_pct": None,
            "days": None,
            "soc_pct": None,
            "months": None,
        }
    enabled = _slot_enabled(slot)
    return {
        "present": True,
        "enabled": enabled,
        "active_now": enabled and _slot_time_in_window(slot, now),
        "start_h": int(getattr(slot, "start_h", 0)),
        "start_m": int(getattr(slot, "start_m", 0)),
        "end_h": int(getattr(slot, "end_h", 0)),
        "end_m": int(getattr(slot, "end_m", 0)),
        "power_pct": _slot_power_pct(slot),
        "days": getattr(slot, "days", None),
        "soc_pct": getattr(slot, "soc", None),
        "months": getattr(slot, "months", None),
    }


async def _connect():
    if not INVERTER_IP:
        raise RuntimeError("INVERTER_IP nie ustawione")
    return await goodwe.connect(INVERTER_IP)


async def _read_one_slot(
    inverter: Any, sid: str, *, now: datetime, supported: bool
) -> tuple[str, dict[str, Any]]:
    if not supported:
        return sid, {"supported": False, **slot_to_payload(None, now=now)}
    try:
        raw = await inverter.read_setting(sid)
    except Exception as e:
        logger.warning("read_setting %s: %s", sid, e)
        return sid, {
            "supported": True,
            "read_error": str(e),
            **slot_to_payload(None, now=now),
        }
    return sid, {"supported": True, **slot_to_payload(raw, now=now)}


async def _fetch_ecoslots_payload_uncached() -> dict[str, Any]:
    now = _local_now()
    inverter = await _connect()
    settings_names = {s.id_ for s in inverter.settings()}
    pairs = await asyncio.gather(
        *[
            _read_one_slot(inverter, sid, now=now, supported=(sid in settings_names))
            for sid in ECO_SETTING_IDS
        ]
    )
    slots = dict(pairs)
    return {
        "inverter_ip": INVERTER_IP,
        "balancing_slot_id": balancing_slot_id(),
        "editable_slot_ids": list(editable_slot_ids()),
        "now": now.isoformat(),
        "slots": slots,
    }


async def fetch_ecoslots_payload() -> dict[str, Any]:
    try:
        return await asyncio.wait_for(
            _fetch_ecoslots_payload_uncached(), timeout=_ECO_READ_TIMEOUT_S
        )
    except asyncio.TimeoutError as e:
        raise TimeoutError(
            f"Odczyt ecoslotów przekroczył {_ECO_READ_TIMEOUT_S:.0f}s (inwerter nie odpowiada?)"
        ) from e


async def write_ecoslot(
    slot_id: str,
    *,
    start_h: int,
    start_m: int,
    end_h: int,
    end_m: int,
    power: int,
    days: str | list[int] = "Mon-Sun",
    soc: int = 100,
    months: str | list[int] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    assert_editable_slot(slot_id)

    async def _do_write() -> dict[str, Any]:
        inverter = await _connect()
        await set_ecoslot(
            inverter,
            slot_id,
            start_h=start_h,
            start_m=start_m,
            end_h=end_h,
            end_m=end_m,
            power=power,
            days=days,
            soc=soc,
            months=months,
            enabled=enabled,
        )
        raw = await inverter.read_setting(slot_id)
        return {"slot_id": slot_id, **slot_to_payload(raw, now=_local_now())}

    try:
        return await asyncio.wait_for(_do_write(), timeout=_ECO_READ_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        raise TimeoutError(
            f"Zapis ecoslotu przekroczył {_ECO_READ_TIMEOUT_S:.0f}s (inwerter nie odpowiada?)"
        ) from e


def run_async(coro):
    return asyncio.run(coro)
