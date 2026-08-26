"""Godzinowe archiwum cech Solcast + OWM pod przyszły model residual/quantile.

Zapis: ``data/planner/pv_features/features_YYYY-MM-DD.json``.

Reguła zamrażania:
- slot przyszły (``now < hour_start``) — upsert świeżego Solcast/OWM;
- od startu godziny — cechy forecastu zamrażamy (zostaje ostatni snapshot sprzed
  godziny, albo pierwszy in-hour gdy nie było wcześniejszego);
- po domknięciu godziny — dopisujemy ``pv_actual_kwh`` z telemetrii.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from guardian_config import TELEMETRY_TZ
from planner.config import PLANNER_PV_FEATURES_DIR, ensure_planner_dirs
from planner.telemetry import hourly_actuals, hourly_pv_meter_delta_kwh
from weather_owm import fetch_weather_pack, hourly_by_local_slot, owm_configured

log = logging.getLogger("planner")

SCHEMA_VERSION = 1
HorizonSlot = tuple[str, int]


class HourFeatureRow(BaseModel):
    schema_version: int = SCHEMA_VERSION
    date: str
    hour: int
    as_of_local: str
    frozen: bool = False
    solcast_p50_kwh: float | None = None
    solcast_p10_kwh: float | None = None
    solcast_p90_kwh: float | None = None
    solcast_source: str | None = None
    owm_clouds: float | None = None
    owm_pop: float | None = None
    owm_temp_c: float | None = None
    owm_weather_id: int | None = None
    owm_weather_main: str | None = None
    owm_weather_description: str | None = None
    owm_rain_1h_mm: float | None = None
    owm_snow_1h_mm: float | None = None
    owm_visibility_m: float | None = None
    owm_from_3h: bool | None = None
    owm_from_current: bool | None = None
    owm_source: str | None = None
    pv_actual_kwh: float | None = None
    pv_actual_method: str | None = None
    pv_actual_power_avg_kwh: float | None = None
    updates: int = 0


class DayFeatureArchive(BaseModel):
    schema_version: int = SCHEMA_VERSION
    date: str
    timezone: str = TELEMETRY_TZ
    updated_at_local: str
    hours: dict[str, HourFeatureRow] = Field(default_factory=dict)


def features_path(local_date: str | date) -> Path:
    ensure_planner_dirs()
    d = local_date if isinstance(local_date, str) else local_date.isoformat()
    return PLANNER_PV_FEATURES_DIR / f"features_{d}.json"


def load_day_archive(local_date: str | date) -> DayFeatureArchive | None:
    path = features_path(local_date)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return DayFeatureArchive.model_validate(raw)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as e:
        log.warning("pv_feature_archive read failed %s: %s", path.name, e)
        return None


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def save_day_archive(archive: DayFeatureArchive) -> Path:
    path = features_path(archive.date)
    _atomic_write(path, archive.model_dump(mode="json"))
    return path


def _hour_key(hour: int) -> str:
    return f"{int(hour):02d}"


def _slot_start(date_iso: str, hour: int) -> datetime:
    return datetime.fromisoformat(f"{date_iso}T{int(hour):02d}:00:00")


def _solcast_fields(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {
            "solcast_p50_kwh": None,
            "solcast_p10_kwh": None,
            "solcast_p90_kwh": None,
            "solcast_source": None,
        }
    p50 = row.get("pv_kw")
    p10 = row.get("pv_kw_p10")
    p90 = row.get("pv_kw_p90")
    return {
        "solcast_p50_kwh": float(p50) if p50 is not None else None,
        "solcast_p10_kwh": float(p10) if p10 is not None else (float(p50) if p50 is not None else None),
        "solcast_p90_kwh": float(p90) if p90 is not None else (float(p50) if p50 is not None else None),
        "solcast_source": str(row.get("source") or "solcast_proxy"),
    }


def _owm_fields(wx: dict[str, Any] | None, *, owm_source: str | None) -> dict[str, Any]:
    if not wx:
        return {
            "owm_clouds": None,
            "owm_pop": None,
            "owm_temp_c": None,
            "owm_weather_id": None,
            "owm_weather_main": None,
            "owm_weather_description": None,
            "owm_rain_1h_mm": None,
            "owm_snow_1h_mm": None,
            "owm_visibility_m": None,
            "owm_from_3h": None,
            "owm_from_current": None,
            "owm_source": owm_source,
        }
    pop = wx.get("pop")
    temp = wx.get("temp")
    wid = wx.get("weather_id")
    vis = wx.get("visibility")
    return {
        "owm_clouds": float(wx["clouds"]) if wx.get("clouds") is not None else None,
        "owm_pop": float(pop) if pop is not None else None,
        "owm_temp_c": float(temp) if temp is not None else None,
        "owm_weather_id": int(wid) if wid is not None else None,
        "owm_weather_main": wx.get("weather_main"),
        "owm_weather_description": wx.get("weather_description"),
        "owm_rain_1h_mm": float(wx.get("rain_1h") or 0.0),
        "owm_snow_1h_mm": float(wx.get("snow_1h") or 0.0),
        "owm_visibility_m": float(vis) if vis is not None else None,
        "owm_from_3h": bool(wx["from_3h"]) if wx.get("from_3h") is not None else None,
        "owm_from_current": bool(wx["from_current"])
        if wx.get("from_current") is not None
        else None,
        "owm_source": owm_source,
    }


def _attach_actuals(row: HourFeatureRow, local_date: date) -> HourFeatureRow:
    if row.pv_actual_kwh is not None:
        return row
    meter = hourly_pv_meter_delta_kwh(local_date)
    power = hourly_actuals(local_date)
    h = int(row.hour)
    data = row.model_dump()
    if h in meter:
        data["pv_actual_kwh"] = float(meter[h])
        data["pv_actual_method"] = "meter_delta_E_pv"
    elif h in power and power[h].get("pv_kwh") is not None:
        data["pv_actual_kwh"] = float(power[h]["pv_kwh"])
        data["pv_actual_method"] = "power_avg"
    if h in power and power[h].get("pv_kwh") is not None:
        data["pv_actual_power_avg_kwh"] = float(power[h]["pv_kwh"])
    return HourFeatureRow.model_validate(data)


def archive_pv_features(
    *,
    now: datetime,
    pv_by_key: dict[HorizonSlot, dict],
    slots: list[HorizonSlot] | None = None,
    weather_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Upsert cech forecastu dla slotów horyzontu (+ wszystkich kluczy z ``pv_by_key``).

    Bezpieczne do wołania co cykl planera. Błędy I/O/OWM nie wywalają planu.
    """
    ensure_planner_dirs()
    now_local = now.replace(tzinfo=None) if now.tzinfo is not None else now
    as_of = now_local.replace(microsecond=0).isoformat()

    keys = list(slots or [])
    seen = set(keys)
    for key in pv_by_key:
        if key not in seen:
            keys.append(key)
            seen.add(key)
    keys.sort()

    wx_by: dict[HorizonSlot, dict[str, Any]] = {}
    owm_source: str | None = None
    try:
        pack = weather_pack
        if pack is None and owm_configured():
            pack = fetch_weather_pack()
        if pack is not None:
            meta = pack.get("_meta") if isinstance(pack, dict) else None
            if isinstance(meta, dict):
                owm_source = str(meta.get("source") or "openweathermap_free_2_5")
                if meta.get("error"):
                    owm_source = f"{owm_source}:{meta.get('error')}"
            else:
                owm_source = "openweathermap_free_2_5"
            wx_by = hourly_by_local_slot(pack)
    except Exception as e:
        log.warning("pv_feature_archive OWM unavailable: %s", e)
        owm_source = f"error:{e}"

    by_date: dict[str, list[HorizonSlot]] = {}
    for slot in keys:
        by_date.setdefault(slot[0], []).append(slot)

    written = 0
    frozen_n = 0
    actuals_n = 0
    paths: list[str] = []

    for date_iso, day_slots in sorted(by_date.items()):
        existing = load_day_archive(date_iso)
        archive = existing or DayFeatureArchive(
            date=date_iso,
            updated_at_local=as_of,
            hours={},
        )
        day_date = date.fromisoformat(date_iso)
        changed = False

        for slot in day_slots:
            _d, hour = slot
            hk = _hour_key(hour)
            hour_start = _slot_start(date_iso, hour)
            hour_end = hour_start + timedelta(hours=1)
            prev = archive.hours.get(hk)

            if prev is not None and prev.frozen:
                updated = _attach_actuals(prev, day_date) if now_local >= hour_end else prev
                if updated.pv_actual_kwh is not None and prev.pv_actual_kwh is None:
                    archive.hours[hk] = updated
                    actuals_n += 1
                    changed = True
                continue

            if now_local >= hour_start:
                # Zamroź cechy: zachowaj poprzedni (pre-hour) snapshot jeśli jest.
                base = prev
                if base is None:
                    base = HourFeatureRow(
                        date=date_iso,
                        hour=hour,
                        as_of_local=as_of,
                        **_solcast_fields(pv_by_key.get(slot)),
                        **_owm_fields(wx_by.get(slot), owm_source=owm_source),
                        updates=1,
                    )
                data = base.model_dump()
                data["frozen"] = True
                row = HourFeatureRow.model_validate(data)
                if now_local >= hour_end:
                    row = _attach_actuals(row, day_date)
                    if row.pv_actual_kwh is not None:
                        actuals_n += 1
                archive.hours[hk] = row
                frozen_n += 1
                written += 1
                changed = True
                continue

            # Przyszły slot — świeży upsert Solcast/OWM.
            row = HourFeatureRow(
                date=date_iso,
                hour=hour,
                as_of_local=as_of,
                frozen=False,
                **_solcast_fields(pv_by_key.get(slot)),
                **_owm_fields(wx_by.get(slot), owm_source=owm_source),
                updates=(prev.updates + 1) if prev is not None else 1,
            )
            archive.hours[hk] = row
            written += 1
            changed = True

        # Domknięte godziny spoza bieżącego horyzontu — dociągnięcie actuali.
        if now_local.date() >= day_date:
            for h in range(24):
                hk = _hour_key(h)
                row = archive.hours.get(hk)
                if row is None:
                    continue
                hour_end = _slot_start(date_iso, h) + timedelta(hours=1)
                if now_local < hour_end:
                    continue
                if not row.frozen:
                    data = row.model_dump()
                    data["frozen"] = True
                    row = HourFeatureRow.model_validate(data)
                    archive.hours[hk] = row
                    frozen_n += 1
                    changed = True
                if row.pv_actual_kwh is None:
                    updated = _attach_actuals(row, day_date)
                    if updated.pv_actual_kwh is not None:
                        archive.hours[hk] = updated
                        actuals_n += 1
                        changed = True

        if changed:
            archive.updated_at_local = as_of
            path = save_day_archive(archive)
            paths.append(str(path))

    summary = {
        "ok": True,
        "slots_seen": len(keys),
        "rows_written": written,
        "rows_frozen": frozen_n,
        "actuals_attached": actuals_n,
        "paths": paths,
        "as_of_local": as_of,
    }
    if written or actuals_n:
        log.info(
            "pv_feature_archive: wrote=%d frozen=%d actuals=%d files=%d",
            written,
            frozen_n,
            actuals_n,
            len(paths),
        )
    return summary


def enrich_actuals_for_date(
    local_date: date | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Dopisz ``pv_actual_kwh`` do domkniętych godzin dnia."""
    d = local_date if isinstance(local_date, date) else date.fromisoformat(local_date)
    archive = load_day_archive(d)
    if archive is None:
        return {"ok": False, "reason": "no_archive", "date": d.isoformat()}
    now_local = now or datetime.now().replace(tzinfo=None)
    if now_local.tzinfo is not None:
        now_local = now_local.replace(tzinfo=None)
    attached = 0
    changed = False
    for hk, row in list(archive.hours.items()):
        hour_end = _slot_start(archive.date, int(row.hour)) + timedelta(hours=1)
        if now_local < hour_end:
            continue
        updated = row
        if not updated.frozen:
            data = updated.model_dump()
            data["frozen"] = True
            updated = HourFeatureRow.model_validate(data)
            changed = True
        before = updated.pv_actual_kwh
        updated = _attach_actuals(updated, d)
        if updated.pv_actual_kwh is not None and before is None:
            attached += 1
            changed = True
        elif updated.model_dump() != row.model_dump():
            changed = True
        archive.hours[hk] = updated
    if changed:
        archive.updated_at_local = now_local.replace(microsecond=0).isoformat()
        path = save_day_archive(archive)
    else:
        path = features_path(d)
    return {
        "ok": True,
        "date": d.isoformat(),
        "actuals_attached": attached,
        "hours": len(archive.hours),
        "path": str(path),
    }
