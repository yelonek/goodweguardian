"""Klient OpenWeatherMap One Call 3.0 (cache na dysku).

Pobiera current + minutely + hourly — pola Tier1 pod korektę PV.
Solar Irradiance (GHI/DNI) nie jest w darmowym One Call.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from guardian_config import (
    DATA_DIR,
    OPENWEATHER_API_KEY,
    OPENWEATHER_CACHE_TTL_S,
    OPENWEATHER_HTTP_TIMEOUT_S,
    OPENWEATHER_LAT,
    OPENWEATHER_LON,
    TELEMETRY_TZ,
)

log = logging.getLogger("guardian")

ONECALL_URL = "https://api.openweathermap.org/data/3.0/onecall"
CACHE_PATH = DATA_DIR / "weather" / "owm_onecall_cache.json"


def owm_configured() -> bool:
    return bool(
        OPENWEATHER_API_KEY
        and OPENWEATHER_LAT is not None
        and OPENWEATHER_LON is not None
    )


def _cache_fresh(path: Path, *, ttl_s: float) -> dict[str, Any] | None:
    if not path.exists() or ttl_s <= 0:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = float(payload.get("_cache_epoch") or 0.0)
        if fetched_at <= 0 or (time.time() - fetched_at) > ttl_s:
            return None
        return payload
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as e:
        log.debug("OWM cache read failed: %s", e)
        return None


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        **data,
        "_cache_epoch": time.time(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def fetch_onecall(
    *,
    force_refresh: bool = False,
    exclude: str = "daily,alerts",
    cache_path: Path | None = None,
) -> dict[str, Any]:
    """
    One Call 3.0 dla skonfigurowanych lat/lon.

    Zwraca dict z polami API + ``_meta`` (cached, fetched_at, error).
    Przy braku konfiguracji / błędzie sieci — ``_meta.error`` i puste bloki.
    """
    path = cache_path or CACHE_PATH
    meta: dict[str, Any] = {
        "source": "openweathermap_onecall_3",
        "cached": False,
        "fetched_at": None,
        "error": None,
        "lat": OPENWEATHER_LAT,
        "lon": OPENWEATHER_LON,
    }

    if not owm_configured():
        meta["error"] = "not_configured"
        return {"_meta": meta, "current": None, "minutely": [], "hourly": []}

    if not force_refresh:
        cached = _cache_fresh(path, ttl_s=OPENWEATHER_CACHE_TTL_S)
        if cached is not None:
            body = {k: v for k, v in cached.items() if not str(k).startswith("_")}
            meta["cached"] = True
            meta["fetched_at"] = cached.get("_cache_epoch")
            body["_meta"] = meta
            return body

    params = {
        "lat": OPENWEATHER_LAT,
        "lon": OPENWEATHER_LON,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "exclude": exclude,
    }
    try:
        with httpx.Client(timeout=OPENWEATHER_HTTP_TIMEOUT_S) as client:
            resp = client.get(ONECALL_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        log.warning("OWM One Call fetch failed: %s", e)
        meta["error"] = str(e)
        stale = None
        if path.exists():
            try:
                stale = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stale = None
        if stale:
            body = {k: v for k, v in stale.items() if not str(k).startswith("_")}
            meta["cached"] = True
            meta["fetched_at"] = stale.get("_cache_epoch")
            meta["error"] = f"fetch_failed_using_stale:{e}"
            body["_meta"] = meta
            return body
        return {"_meta": meta, "current": None, "minutely": [], "hourly": []}

    try:
        _write_cache(path, data)
    except OSError as e:
        log.debug("OWM cache write failed: %s", e)

    meta["fetched_at"] = time.time()
    data["_meta"] = meta
    return data


def hourly_by_local_slot(
    onecall: dict[str, Any],
    *,
    tz_name: str = TELEMETRY_TZ,
) -> dict[tuple[str, int], dict[str, Any]]:
    """Mapuje ``hourly[]`` One Call → ``(date_iso, hour)`` w ``TELEMETRY_TZ``."""
    tz = ZoneInfo(tz_name)
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for item in onecall.get("hourly") or []:
        try:
            dt = datetime.fromtimestamp(int(item["dt"]), tz=tz)
            key = (dt.date().isoformat(), dt.hour)
            weather0 = (item.get("weather") or [{}])[0]
            rain = item.get("rain") or {}
            snow = item.get("snow") or {}
            out[key] = {
                "dt": int(item["dt"]),
                "local_date": key[0],
                "local_hour": key[1],
                "temp": float(item["temp"]) if item.get("temp") is not None else None,
                "clouds": float(item.get("clouds") or 0.0),
                "uvi": float(item["uvi"]) if item.get("uvi") is not None else None,
                "pop": float(item.get("pop") or 0.0),
                "visibility": item.get("visibility"),
                "weather_id": int(weather0["id"]) if weather0.get("id") is not None else None,
                "weather_main": weather0.get("main"),
                "weather_description": weather0.get("description"),
                "rain_1h": float(rain.get("1h") or 0.0),
                "snow_1h": float(snow.get("1h") or 0.0),
            }
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return out


def minutely_mean_precip_mmh(onecall: dict[str, Any]) -> float | None:
    """Średnie ``minutely.precipitation`` [mm/h] na najbliższą godzinę."""
    rows = onecall.get("minutely") or []
    if not rows:
        return None
    vals: list[float] = []
    for row in rows:
        try:
            vals.append(float(row.get("precipitation") or 0.0))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None
    return sum(vals) / len(vals)


def current_tier1(onecall: dict[str, Any]) -> dict[str, Any] | None:
    cur = onecall.get("current")
    if not isinstance(cur, dict):
        return None
    weather0 = (cur.get("weather") or [{}])[0]
    rain = cur.get("rain") or {}
    return {
        "dt": cur.get("dt"),
        "temp": cur.get("temp"),
        "clouds": float(cur.get("clouds") or 0.0),
        "uvi": float(cur["uvi"]) if cur.get("uvi") is not None else None,
        "visibility": cur.get("visibility"),
        "weather_id": int(weather0["id"]) if weather0.get("id") is not None else None,
        "weather_main": weather0.get("main"),
        "weather_description": weather0.get("description"),
        "rain_1h": float(rain.get("1h") or 0.0),
        "pop": None,
    }
