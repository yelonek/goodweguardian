"""Append-only JSONL telemetrii (jeden plik na dzień lokalny)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from guardian_config import TELEMETRY_DIR, TELEMETRY_TZ


class CycleTelemetryRecord(BaseModel):
    schema_version: int = 1
    ts_utc: str
    local_date: str
    local_hour: int = Field(ge=0, le=23)
    local_minute: int = Field(ge=0, le=59)
    weekday: int = Field(ge=0, le=6)
    is_weekend: bool
    grid_w: float
    pv_w: float
    battery_w: float
    consumption_w: float
    soc_pct: float
    E_imp_kwh: float
    E_exp_kwh: float
    e_day_imp_kwh: float | None = None
    e_day_exp_kwh: float | None = None
    remaining_kwh: float
    time_to_end_s: float
    delta_imp_kwh: float
    delta_exp_kwh: float
    slot_balancing_active: bool
    other_eco_active: bool
    ecoslot_pct: int | None = None
    watchdog_write_slot: bool
    watchdog_reason: str
    guardian_control_enabled: bool
    control_source: str
    cmd_enabled: bool
    cmd_pct: int
    cmd_duration_s: float


def append_cycle_record(record: CycleTelemetryRecord) -> None:
    log = logging.getLogger("guardian")
    try:
        TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        path = TELEMETRY_DIR / f"telemetry_{record.local_date}.jsonl"
        line = record.model_dump_json() + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except OSError as e:
        log.warning("telemetry append failed: %s", e)


def build_ts_and_calendar(now: datetime) -> tuple[str, str, int, int, int, bool]:
    """now = naive local clock (jak datetime.now() w runnerze)."""
    ts_utc = datetime.now(timezone.utc).isoformat()
    try:
        tz = ZoneInfo(TELEMETRY_TZ)
        loc = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
    except Exception:
        loc = now
    local_date = loc.date().isoformat()
    return (
        ts_utc,
        local_date,
        loc.hour,
        loc.minute,
        loc.weekday(),
        loc.weekday() >= 5,
    )
