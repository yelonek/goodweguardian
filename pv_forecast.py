"""PV forecast loader z Solcast proxy + agregacja do godzin lokalnych.

Proxy udostępnia:
- ``/forecasts``  — bieżący snapshot prognozy do przodu (sloty 30 min).
- ``/history``    — historyczne prognozy (te same sloty 30 min, z ``fetched_at``).

Dla zakończonych godzin lokalnych wybieramy snapshot z najnowszym ``fetched_at``
**przed** początkiem godziny (prognoza znana przed :00), nie średnią wszystkich fetchy.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from guardian_config import PROXY_HTTP_TIMEOUT_S, SOLCAST_PROXY_BASE_URL, TELEMETRY_TZ

log = logging.getLogger("guardian")


def _parse_dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _parse_fetched_at(ts: str) -> datetime:
    """``fetched_at`` z proxy bywa bez strefy — traktujemy jako ``TELEMETRY_TZ``."""
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    tz = ZoneInfo(TELEMETRY_TZ)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _to_local_hour(period_end: str) -> tuple[str, int]:
    dt_utc = _parse_dt(period_end)
    dt_loc = dt_utc.astimezone(ZoneInfo(TELEMETRY_TZ))
    return dt_loc.date().isoformat(), dt_loc.hour


def _local_hour_start(date_s: str, hour: int) -> datetime:
    return datetime.fromisoformat(f"{date_s}T{hour:02d}:00:00").replace(
        tzinfo=ZoneInfo(TELEMETRY_TZ)
    )


def _aggregate_items_to_hours(items: list[dict]) -> list[dict]:
    """Agreguje sloty (zawsze 30 min) do średniej mocy [kW] w godzinie lokalnej."""
    buckets: dict[tuple[str, int], dict[str, float]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        pe = item.get("period_end")
        if not isinstance(pe, str):
            continue
        try:
            date_s, hour = _to_local_hour(pe)
        except (ValueError, IndexError):
            continue
        key = (date_s, hour)
        b = buckets.setdefault(key, {"sum": 0.0, "sum10": 0.0, "sum90": 0.0, "count": 0.0})
        try:
            b["sum"] += float(item.get("pv_estimate", 0.0))
            b["sum10"] += float(item.get("pv_estimate10", 0.0))
            b["sum90"] += float(item.get("pv_estimate90", 0.0))
            b["count"] += 1.0
        except (TypeError, ValueError):
            continue

    rows: list[dict] = []
    for (date_s, hour), agg in sorted(buckets.items()):
        if agg["count"] <= 0:
            continue
        rows.append(
            {
                "date": date_s,
                "hour": hour,
                "pv_kw": agg["sum"] / agg["count"],
                "pv_kw_p10": agg["sum10"] / agg["count"],
                "pv_kw_p90": agg["sum90"] / agg["count"],
                "samples": int(agg["count"]),
            }
        )
    return rows


def _aggregate_single_hour(items: list[dict], *, date_s: str, hour: int) -> dict | None:
    rows = _aggregate_items_to_hours(items)
    for row in rows:
        if row["date"] == date_s and int(row["hour"]) == hour:
            return row
    return None


def _fetch_forecasts_items(client: httpx.Client) -> tuple[list[dict], dict[str, Any]]:
    r = client.get(f"{SOLCAST_PROXY_BASE_URL}/forecasts")
    r.raise_for_status()
    payload = r.json()
    items = payload.get("data", {}).get("forecasts", [])
    if not isinstance(items, list):
        items = []
    return [x for x in items if isinstance(x, dict)], payload


def _fetch_history_items(
    *,
    client: httpx.Client,
    start_local: datetime,
    end_local: datetime,
    limit: int,
) -> list[dict]:
    """``/history`` — zakres ``period_end`` w czasie lokalnym (``start``/``end`` + ``tz``)."""
    tz = ZoneInfo(TELEMETRY_TZ)
    start = (
        start_local.replace(tzinfo=tz)
        if start_local.tzinfo is None
        else start_local.astimezone(tz)
    )
    end = (
        end_local.replace(tzinfo=tz)
        if end_local.tzinfo is None
        else end_local.astimezone(tz)
    )
    params = {
        "start": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "tz": TELEMETRY_TZ,
        "limit": int(limit),
    }
    r = client.get(f"{SOLCAST_PROXY_BASE_URL}/history", params=params)
    r.raise_for_status()
    payload = r.json()
    items = payload.get("forecasts", [])
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict)]


def _items_for_local_hour(items: list[dict], *, date_s: str, hour: int) -> list[dict]:
    out: list[dict] = []
    for item in items:
        pe = item.get("period_end")
        if not isinstance(pe, str):
            continue
        try:
            d, h = _to_local_hour(pe)
        except (ValueError, IndexError):
            continue
        if d == date_s and h == hour:
            out.append(item)
    return out


def _pre_hour_forecast_items(
    history_items: list[dict],
    *,
    date_s: str,
    hour: int,
) -> list[dict]:
    """
    Sloty z prognozy z ostatniego ``fetched_at`` przed początkiem godziny lokalnej.

    Wybieramy snapshot sprzed :00, nie średnią wszystkich historycznych fetchy.
    """
    slot_start = _local_hour_start(date_s, hour)
    in_hour: list[dict] = []
    for item in history_items:
        try:
            d, h = _to_local_hour(str(item.get("period_end")))
        except (ValueError, IndexError, TypeError):
            continue
        if d != date_s or h != hour:
            continue
        try:
            fetched = _parse_fetched_at(str(item.get("fetched_at")))
        except (ValueError, TypeError):
            continue
        if fetched < slot_start:
            in_hour.append(item)
    if not in_hour:
        return []
    best_fetched = max(_parse_fetched_at(str(i["fetched_at"])) for i in in_hour)
    return [
        i
        for i in in_hour
        if _parse_fetched_at(str(i["fetched_at"])) == best_fetched
    ]


def _hour_slots_between(
    start_local: datetime,
    end_local: datetime,
) -> list[tuple[str, int]]:
    tz = ZoneInfo(TELEMETRY_TZ)
    if start_local.tzinfo is None:
        cur = start_local.replace(tzinfo=tz)
    else:
        cur = start_local.astimezone(tz)
    if end_local.tzinfo is None:
        end = end_local.replace(tzinfo=tz)
    else:
        end = end_local.astimezone(tz)
    cur = cur.replace(minute=0, second=0, microsecond=0)
    out: list[tuple[str, int]] = []
    while cur < end:
        out.append((cur.date().isoformat(), cur.hour))
        cur += timedelta(hours=1)
    return out


def fetch_hourly_pv_forecast(hours: int = 48) -> dict:
    """
    Pobiera ``/forecasts`` i agreguje 30-min sloty do godzin lokalnych. Tylko przyszłość.
    """
    if not SOLCAST_PROXY_BASE_URL:
        raise RuntimeError("SOLCAST_PROXY_BASE_URL is empty")

    with httpx.Client(timeout=PROXY_HTTP_TIMEOUT_S) as client:
        items, payload = _fetch_forecasts_items(client)

    rows = _aggregate_items_to_hours(items)
    return {
        "timezone": TELEMETRY_TZ,
        "source": "solcast_proxy",
        "cached": bool(payload.get("cached", False)),
        "fetched_at": payload.get("fetched_at"),
        "hours": rows[: max(1, hours)],
    }


def fetch_hourly_pv_forecast_with_history(
    *,
    hours_back: int = 48,
    hours_forward: int = 48,
    now: datetime | None = None,
) -> dict:
    """
    Łączy ``/history`` (zakończone godziny) z ``/forecasts`` (bieżąca i przyszłe).

    Zakończona godzina H: prognoza z najnowszego ``fetched_at`` < H:00 lokalnego.
    Bieżąca / przyszła godzina: bieżący snapshot ``/forecasts``.
    """
    if not SOLCAST_PROXY_BASE_URL:
        raise RuntimeError("SOLCAST_PROXY_BASE_URL is empty")

    tz = ZoneInfo(TELEMETRY_TZ)
    now_local = (
        datetime.now(tz)
        if now is None
        else (now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz))
    )

    window_start = now_local - timedelta(hours=max(1, hours_back))
    window_end = now_local + timedelta(hours=max(0, hours_forward) + 1)
    history_limit = max(1000, 2 * (hours_back + hours_forward + 2) + 64)

    history_items: list[dict] = []
    forecasts_items: list[dict] = []
    forecasts_payload: dict[str, Any] = {}

    with httpx.Client(timeout=PROXY_HTTP_TIMEOUT_S) as client:
        try:
            forecasts_items, forecasts_payload = _fetch_forecasts_items(client)
        except httpx.HTTPError as e:
            log.warning("solcast /forecasts fetch failed: %s", e)
        try:
            history_items = _fetch_history_items(
                client=client,
                start_local=window_start,
                end_local=window_end,
                limit=history_limit,
            )
        except httpx.HTTPError as e:
            log.warning("solcast /history fetch failed: %s", e)

    rows: list[dict] = []
    for date_s, hour in _hour_slots_between(window_start, window_end):
        slot_end = _local_hour_start(date_s, hour) + timedelta(hours=1)
        hour_complete = slot_end <= now_local
        if hour_complete:
            items = _pre_hour_forecast_items(history_items, date_s=date_s, hour=hour)
        else:
            items = _items_for_local_hour(forecasts_items, date_s=date_s, hour=hour)
        row = _aggregate_single_hour(items, date_s=date_s, hour=hour)
        if row is not None:
            rows.append(row)

    return {
        "timezone": TELEMETRY_TZ,
        "source": "solcast_proxy_history+forecast",
        "cached": bool(forecasts_payload.get("cached", False)),
        "fetched_at": forecasts_payload.get("fetched_at"),
        "history_items": len(history_items),
        "forecast_items": len(forecasts_items),
        "hours_back": hours_back,
        "hours_forward": hours_forward,
        "hours": rows,
    }
