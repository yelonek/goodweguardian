"""PV forecast loader z Solcast proxy + agregacja do godzin lokalnych.

Proxy udostępnia:
- ``/forecasts``  — bieżący snapshot prognozy do przodu (sloty 30 min).
- ``/history``    — historyczne prognozy (te same sloty 30 min, z ``fetched_at``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from guardian_config import PROXY_HTTP_TIMEOUT_S, SOLCAST_PROXY_BASE_URL, TELEMETRY_TZ

log = logging.getLogger("guardian")


def _parse_dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _to_local_hour(period_end: str) -> tuple[str, int]:
    dt_utc = _parse_dt(period_end)
    dt_loc = dt_utc.astimezone(ZoneInfo(TELEMETRY_TZ))
    return dt_loc.date().isoformat(), dt_loc.hour


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
    start_utc: datetime,
    end_utc: datetime,
    limit: int,
) -> list[dict]:
    """``/history`` z proxy. Zwraca listę rekordów (lub [] gdy proxy nie wspiera)."""
    params = {
        "start": start_utc.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
        "end": end_utc.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
        "limit": int(limit),
    }
    r = client.get(f"{SOLCAST_PROXY_BASE_URL}/history", params=params)
    r.raise_for_status()
    payload = r.json()
    items = payload.get("forecasts", [])
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict)]


def _dedupe_history_keep_latest(items: list[dict]) -> dict[str, dict]:
    """Jeden rekord na ``period_end`` — najnowszy ``fetched_at`` (string ISO porównywalny leksykograficznie)."""
    out: dict[str, dict] = {}
    for item in items:
        pe = item.get("period_end")
        if not isinstance(pe, str):
            continue
        prev = out.get(pe)
        if prev is None:
            out[pe] = item
            continue
        prev_at = str(prev.get("fetched_at") or "")
        cur_at = str(item.get("fetched_at") or "")
        if cur_at > prev_at:
            out[pe] = item
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
    Łączy ``/history`` (przeszłe sloty) z ``/forecasts`` (przyszłość) i agreguje do godzin
    lokalnych. Dla tego samego ``period_end`` ``/forecasts`` wygrywa nad ``/history``;
    w obrębie ``/history`` brany jest najnowszy ``fetched_at``.
    """
    if not SOLCAST_PROXY_BASE_URL:
        raise RuntimeError("SOLCAST_PROXY_BASE_URL is empty")

    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    start_utc = now_utc - timedelta(hours=max(1, hours_back))
    end_utc = now_utc + timedelta(minutes=5)

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
                start_utc=start_utc,
                end_utc=end_utc,
                limit=4 * max(1, hours_back) + 8,
            )
        except httpx.HTTPError as e:
            log.warning("solcast /history fetch failed: %s", e)

    by_period: dict[str, dict] = _dedupe_history_keep_latest(history_items)
    for item in forecasts_items:
        pe = item.get("period_end")
        if isinstance(pe, str):
            by_period[pe] = item

    forward_cutoff_utc = now_utc + timedelta(hours=max(0, hours_forward))
    history_cutoff_utc = now_utc - timedelta(hours=max(0, hours_back))
    filtered: list[dict] = []
    for item in by_period.values():
        pe = item.get("period_end")
        if not isinstance(pe, str):
            continue
        try:
            slot_utc = _parse_dt(pe).astimezone(UTC)
        except ValueError:
            continue
        if slot_utc < history_cutoff_utc or slot_utc > forward_cutoff_utc:
            continue
        filtered.append(item)

    rows = _aggregate_items_to_hours(filtered)
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
