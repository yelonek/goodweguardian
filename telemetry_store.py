"""Append-only JSONL telemetrii (jeden plik na dzień lokalny)."""

from __future__ import annotations

import logging
import json
from datetime import datetime, timedelta, timezone
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
    E_pv_kwh: float | None = None
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
    planner_execution_enabled: bool = False
    planner_execution_source: str = "env"
    plan_target_net_kwh: float | None = None
    exec_mode: str | None = None
    plan_id: str | None = None


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


def recent_consumption_average_w(now: datetime, window_minutes: int) -> float | None:
    """Średnia consumption_w z ostatnich N minut telemetrii."""
    if window_minutes <= 0:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    local_dates = {
        now.date().isoformat(),
        (now - timedelta(days=1)).date().isoformat(),
    }
    total = 0.0
    count = 0

    for local_date in local_dates:
        path = TELEMETRY_DIR / f"telemetry_{local_date}.jsonl"
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        ts = datetime.fromisoformat(str(data["ts_utc"]))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts < cutoff:
                            continue
                        value = float(data["consumption_w"])
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    total += value
                    count += 1
        except OSError:
            logging.getLogger("guardian").debug(
                "telemetry average read failed: %s", path
            )

    if count == 0:
        return None
    return total / count


def hour_start_counters_from_telemetry(now: datetime) -> tuple[float, float] | None:
    """
    Pierwszy odczyt E_exp_kwh / E_imp_kwh w bieżącej lokalnej godzinie (z JSONL).
    Użyte przy starcie guardiana w środku godziny po migracji / restarcie.
    """
    path = TELEMETRY_DIR / f"telemetry_{now.strftime('%Y-%m-%d')}.jsonl"
    if not path.exists():
        return None
    target_hour = now.hour
    best_minute: int | None = None
    exp_start: float | None = None
    imp_start: float | None = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if int(row["local_hour"]) != target_hour:
                        continue
                    minute = int(row["local_minute"])
                    exp = float(row["E_exp_kwh"])
                    imp = float(row["E_imp_kwh"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if best_minute is None or minute < best_minute:
                    best_minute = minute
                    exp_start = exp
                    imp_start = imp
    except OSError:
        return None
    if exp_start is None or imp_start is None:
        return None
    return exp_start, imp_start


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
