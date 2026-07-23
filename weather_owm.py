"""Klient OpenWeatherMap — darmowy plan Current + 5-day/3-hour forecast.

Endpointy (Free):
- ``/data/2.5/weather`` — current (clouds, weather, rain.1h, …; bez uvi)
- ``/data/2.5/forecast`` — kroki 3h z ``pop``, ``clouds``, ``rain.3h`` / ``snow.3h``

Kroki 3h są rozwijane do godzin lokalnych (każdy punkt pokrywa [dt, dt+3h)).
Brak minutely / uvi (One Call) — korekta PV i tak toleruje ``uvi=None``.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
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

CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
CACHE_PATH = DATA_DIR / "weather" / "owm_free_cache.json"


def owm_configured() -> bool:
    return bool(
        OPENWEATHER_API_KEY
        and OPENWEATHER_LAT is not None
        and OPENWEATHER_LON is not None
    )


def _http_err_label(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return f"RequestError:{type(exc).__name__}"
    return type(exc).__name__


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
    envelope = {**data, "_cache_epoch": time.time()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _base_params() -> dict[str, Any]:
    return {
        "lat": OPENWEATHER_LAT,
        "lon": OPENWEATHER_LON,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
    }


def _normalize_current(raw: dict[str, Any]) -> dict[str, Any]:
    """Sprowadza Current 2.5 do płaskiego kształtu zbliżonego do One Call ``current``."""
    weather = list(raw.get("weather") or [])
    weather0 = weather[0] if weather else {}
    main = raw.get("main") or {}
    clouds = raw.get("clouds") or {}
    rain = raw.get("rain") or {}
    snow = raw.get("snow") or {}
    clouds_all = clouds.get("all") if isinstance(clouds, dict) else clouds
    return {
        "dt": raw.get("dt"),
        "temp": main.get("temp") if main else raw.get("temp"),
        "clouds": float(clouds_all or 0.0),
        "uvi": None,
        "visibility": raw.get("visibility"),
        "weather": weather,
        "weather_id": int(weather0["id"]) if weather0.get("id") is not None else None,
        "weather_main": weather0.get("main"),
        "weather_description": weather0.get("description"),
        "rain": {"1h": float(rain.get("1h") or 0.0)} if rain else {},
        "snow": {"1h": float(snow.get("1h") or 0.0)} if snow else {},
        "rain_1h": float(rain.get("1h") or 0.0),
        "pop": None,
        "_raw": True,
    }


def _forecast_item_to_row(
    item: dict[str, Any],
    *,
    tz: ZoneInfo,
) -> dict[str, Any] | None:
    try:
        dt_unix = int(item["dt"])
        dt_loc = datetime.fromtimestamp(dt_unix, tz=tz)
        weather0 = (item.get("weather") or [{}])[0]
        clouds = item.get("clouds") or {}
        rain = item.get("rain") or {}
        snow = item.get("snow") or {}
        rain_3h = float(rain.get("3h") or 0.0)
        snow_3h = float(snow.get("3h") or 0.0)
        return {
            "dt": dt_unix,
            "local_date": dt_loc.date().isoformat(),
            "local_hour": dt_loc.hour,
            "temp": float((item.get("main") or {}).get("temp"))
            if (item.get("main") or {}).get("temp") is not None
            else None,
            "clouds": float(clouds.get("all") or 0.0),
            "uvi": None,
            "pop": float(item.get("pop") or 0.0),
            "visibility": item.get("visibility"),
            "weather_id": int(weather0["id"]) if weather0.get("id") is not None else None,
            "weather_main": weather0.get("main"),
            "weather_description": weather0.get("description"),
            # 3h volume → approx mm/h for Tier1 rain factor
            "rain_1h": rain_3h / 3.0,
            "snow_1h": snow_3h / 3.0,
            "rain_3h": rain_3h,
            "snow_3h": snow_3h,
            "from_3h": True,
        }
    except (KeyError, TypeError, ValueError, OSError):
        return None


def expand_forecast_3h_to_hourly(
    forecast_list: list[dict[str, Any]],
    *,
    tz_name: str = TELEMETRY_TZ,
) -> list[dict[str, Any]]:
    """
    Każdy punkt 3h o czasie ``dt`` pokrywa lokalne godziny ``[dt, dt+3h)``.
    """
    tz = ZoneInfo(tz_name)
    by_slot: dict[tuple[str, int], dict[str, Any]] = {}
    for item in forecast_list:
        row = _forecast_item_to_row(item, tz=tz)
        if row is None:
            continue
        start = datetime.fromtimestamp(int(row["dt"]), tz=tz).replace(
            minute=0, second=0, microsecond=0
        )
        for i in range(3):
            slot_dt = start + timedelta(hours=i)
            key = (slot_dt.date().isoformat(), slot_dt.hour)
            by_slot[key] = {
                **row,
                "local_date": key[0],
                "local_hour": key[1],
                "slot_dt": int(slot_dt.timestamp()),
            }
    return [by_slot[k] for k in sorted(by_slot.keys())]


def fetch_weather_pack(
    *,
    force_refresh: bool = False,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    """
    Pobiera Current + 5-day/3h forecast i składa pack z ``current`` / ``hourly``.

    ``hourly`` to rozwinięcie kroków 3h do godzin lokalnych (bez uvi/minutely).
    """
    path = cache_path or CACHE_PATH
    meta: dict[str, Any] = {
        "source": "openweathermap_free_2_5",
        "cached": False,
        "fetched_at": None,
        "error": None,
        "lat": OPENWEATHER_LAT,
        "lon": OPENWEATHER_LON,
    }

    empty = {"_meta": meta, "current": None, "minutely": [], "hourly": [], "forecast_3h": []}

    if not owm_configured():
        meta["error"] = "not_configured"
        return empty

    if not force_refresh:
        cached = _cache_fresh(path, ttl_s=OPENWEATHER_CACHE_TTL_S)
        if cached is not None:
            body = {k: v for k, v in cached.items() if not str(k).startswith("_")}
            meta["cached"] = True
            meta["fetched_at"] = cached.get("_cache_epoch")
            body["_meta"] = meta
            return body

    params = _base_params()
    try:
        with httpx.Client(timeout=OPENWEATHER_HTTP_TIMEOUT_S) as client:
            cur_resp = client.get(CURRENT_URL, params=params)
            cur_resp.raise_for_status()
            forecast_resp = client.get(FORECAST_URL, params=params)
            forecast_resp.raise_for_status()
            current_raw = cur_resp.json()
            forecast_raw = forecast_resp.json()
    except (httpx.HTTPError, ValueError, TypeError) as e:
        err = _http_err_label(e)
        log.warning("OWM free fetch failed: %s", err)
        meta["error"] = err
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
            meta["error"] = f"fetch_failed_using_stale:{err}"
            body["_meta"] = meta
            return body
        return empty

    current = _normalize_current(current_raw if isinstance(current_raw, dict) else {})
    forecast_list = list((forecast_raw or {}).get("list") or [])
    hourly = expand_forecast_3h_to_hourly(forecast_list)

    # Nakładka current na bieżącą godzinę lokalną (świeższe clouds/weather/rain.1h).
    if current.get("dt") is not None:
        try:
            tz = ZoneInfo(TELEMETRY_TZ)
            dt_loc = datetime.fromtimestamp(int(current["dt"]), tz=tz)
            key = (dt_loc.date().isoformat(), dt_loc.hour)
            overlay = {
                "dt": int(current["dt"]),
                "local_date": key[0],
                "local_hour": key[1],
                "temp": current.get("temp"),
                "clouds": float(current.get("clouds") or 0.0),
                "uvi": None,
                "pop": None,
                "visibility": current.get("visibility"),
                "weather_id": current.get("weather_id"),
                "weather_main": current.get("weather_main"),
                "weather_description": current.get("weather_description"),
                "rain_1h": float(current.get("rain_1h") or 0.0),
                "snow_1h": 0.0,
                "from_3h": False,
                "from_current": True,
            }
            replaced = False
            for i, row in enumerate(hourly):
                if (row.get("local_date"), row.get("local_hour")) == key:
                    hourly[i] = overlay
                    replaced = True
                    break
            if not replaced:
                hourly.append(overlay)
                hourly.sort(key=lambda r: (r["local_date"], int(r["local_hour"])))
        except (TypeError, ValueError, OSError):
            pass

    data = {
        "current": current,
        "minutely": [],
        "hourly": hourly,
        "forecast_3h": forecast_list,
    }
    try:
        _write_cache(path, data)
    except OSError as e:
        log.debug("OWM cache write failed: %s", e)

    meta["fetched_at"] = time.time()
    data["_meta"] = meta
    return data


def fetch_onecall(**kwargs: Any) -> dict[str, Any]:
    """Alias wsteczny — pack jest teraz z Free 2.5, nie One Call 3.0."""
    return fetch_weather_pack(**kwargs)


def hourly_by_local_slot(
    pack: dict[str, Any],
    *,
    tz_name: str = TELEMETRY_TZ,
) -> dict[tuple[str, int], dict[str, Any]]:
    """Mapuje ``hourly[]`` (już rozwinięte) → ``(date_iso, hour)``."""
    out: dict[tuple[str, int], dict[str, Any]] = {}
    hourly = pack.get("hourly") or []
    if hourly:
        for item in hourly:
            try:
                if "local_date" in item and "local_hour" in item:
                    key = (str(item["local_date"]), int(item["local_hour"]))
                else:
                    tz = ZoneInfo(tz_name)
                    dt = datetime.fromtimestamp(int(item["dt"]), tz=tz)
                    key = (dt.date().isoformat(), dt.hour)
                out[key] = {
                    "dt": int(item.get("dt") or 0),
                    "local_date": key[0],
                    "local_hour": key[1],
                    "temp": item.get("temp"),
                    "clouds": float(item.get("clouds") or 0.0),
                    "uvi": float(item["uvi"]) if item.get("uvi") is not None else None,
                    "pop": float(item["pop"]) if item.get("pop") is not None else None,
                    "visibility": item.get("visibility"),
                    "weather_id": int(item["weather_id"])
                    if item.get("weather_id") is not None
                    else None,
                    "weather_main": item.get("weather_main"),
                    "weather_description": item.get("weather_description"),
                    "rain_1h": float(item.get("rain_1h") or 0.0),
                    "snow_1h": float(item.get("snow_1h") or 0.0),
                    "from_3h": bool(item.get("from_3h")),
                    "from_current": bool(item.get("from_current")),
                }
            except (KeyError, TypeError, ValueError, OSError):
                continue
        return out

    # Fallback: surowy forecast_3h
    for row in expand_forecast_3h_to_hourly(list(pack.get("forecast_3h") or []), tz_name=tz_name):
        out[(row["local_date"], int(row["local_hour"]))] = row
    return out


def minutely_mean_precip_mmh(pack: dict[str, Any]) -> float | None:
    """Free plan nie ma minutely — zawsze ``None``."""
    rows = pack.get("minutely") or []
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


def current_tier1(pack: dict[str, Any]) -> dict[str, Any] | None:
    cur = pack.get("current")
    if not isinstance(cur, dict):
        return None
    # Już znormalizowany albo surowy 2.5 / One Call
    if cur.get("weather_id") is not None or cur.get("_raw"):
        return {
            "dt": cur.get("dt"),
            "temp": cur.get("temp"),
            "clouds": float(cur.get("clouds") or 0.0),
            "uvi": cur.get("uvi"),
            "visibility": cur.get("visibility"),
            "weather_id": cur.get("weather_id"),
            "weather_main": cur.get("weather_main"),
            "weather_description": cur.get("weather_description"),
            "rain_1h": float(cur.get("rain_1h") or 0.0),
            "pop": cur.get("pop"),
        }
    weather0 = (cur.get("weather") or [{}])[0]
    rain = cur.get("rain") or {}
    clouds = cur.get("clouds")
    if isinstance(clouds, dict):
        clouds_v = float(clouds.get("all") or 0.0)
    else:
        clouds_v = float(clouds or 0.0)
    return {
        "dt": cur.get("dt"),
        "temp": cur.get("temp"),
        "clouds": clouds_v,
        "uvi": float(cur["uvi"]) if cur.get("uvi") is not None else None,
        "visibility": cur.get("visibility"),
        "weather_id": int(weather0["id"]) if weather0.get("id") is not None else None,
        "weather_main": weather0.get("main"),
        "weather_description": weather0.get("description"),
        "rain_1h": float(rain.get("1h") or 0.0),
        "pop": None,
    }
