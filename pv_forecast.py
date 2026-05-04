"""PV forecast loader z Solcast proxy + agregacja do godzin."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from guardian_config import PROXY_HTTP_TIMEOUT_S, SOLCAST_PROXY_BASE_URL, TELEMETRY_TZ


def _parse_dt(ts: str) -> datetime:
    # Solcast proxy zwraca UTC z sufiksem Z i czasem 7-cyfrowym.
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _to_local_hour(period_end: str) -> tuple[str, int]:
    dt_utc = _parse_dt(period_end)
    dt_loc = dt_utc.astimezone(ZoneInfo(TELEMETRY_TZ))
    return dt_loc.date().isoformat(), dt_loc.hour


def fetch_hourly_pv_forecast(hours: int = 48) -> dict:
    """
    Pobiera forecast z proxy i agreguje punkty 30m do godzin lokalnych.
    Wartości to średnia mocy w godzinie [kW].
    """
    if not SOLCAST_PROXY_BASE_URL:
        raise RuntimeError("SOLCAST_PROXY_BASE_URL is empty")

    with httpx.Client(timeout=PROXY_HTTP_TIMEOUT_S) as client:
        r = client.get(f"{SOLCAST_PROXY_BASE_URL}/forecasts")
        r.raise_for_status()
        payload = r.json()

    forecasts = payload.get("data", {}).get("forecasts", [])
    buckets: dict[tuple[str, int], dict[str, float]] = {}
    for item in forecasts:
        if not isinstance(item, dict):
            continue
        period_end = item.get("period_end")
        if not isinstance(period_end, str):
            continue
        date_s, hour = _to_local_hour(period_end)
        key = (date_s, hour)
        b = buckets.setdefault(
            key, {"sum": 0.0, "sum10": 0.0, "sum90": 0.0, "count": 0.0}
        )
        try:
            b["sum"] += float(item.get("pv_estimate", 0.0))
            b["sum10"] += float(item.get("pv_estimate10", 0.0))
            b["sum90"] += float(item.get("pv_estimate90", 0.0))
            b["count"] += 1.0
        except (TypeError, ValueError):
            continue

    rows = []
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
            }
        )

    return {
        "timezone": TELEMETRY_TZ,
        "source": "solcast_proxy",
        "cached": bool(payload.get("cached", False)),
        "fetched_at": payload.get("fetched_at"),
        "hours": rows[: max(1, hours)],
    }
