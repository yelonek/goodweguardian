"""Persystencja deklaracji planowanego ładowania EV."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ev_charging_plan import EvChargingDeclaration, EvChargingPlan, build_ev_charging_plan
from guardian_config import EV_CHARGING_DECLARATION_PATH, TELEMETRY_TZ

log = logging.getLogger("guardian")


def _local_today() -> date:
    return datetime.now(ZoneInfo(TELEMETRY_TZ)).date()


def read_declaration(path: Path | None = None) -> EvChargingDeclaration | None:
    target = path or EV_CHARGING_DECLARATION_PATH
    if not target.exists():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        decl = EvChargingDeclaration.model_validate(data)
        if decl.date != _local_today().isoformat():
            return None
        return decl
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        log.warning("ev_charging declaration read failed %s: %s", target, e)
        return None


def write_declaration(
    declaration: EvChargingDeclaration,
    path: Path | None = None,
) -> EvChargingDeclaration:
    target = path or EV_CHARGING_DECLARATION_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(ZoneInfo(TELEMETRY_TZ)).isoformat()
    payload = declaration.model_copy(update={"updated_at": now_iso})
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(payload.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return payload


def clear_declaration(path: Path | None = None) -> None:
    target = path or EV_CHARGING_DECLARATION_PATH
    if target.exists():
        target.unlink()


def active_plan(path: Path | None = None) -> EvChargingPlan:
    decl = read_declaration(path=path)
    return build_ev_charging_plan(declaration=decl)


def declarations_for_dates(dates: set[str]) -> dict[str, EvChargingDeclaration]:
    today = _local_today().isoformat()
    if today not in dates:
        return {}
    decl = read_declaration()
    if decl is None:
        return {}
    return {decl.date: decl}
