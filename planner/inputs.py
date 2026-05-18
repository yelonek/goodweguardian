"""Zbieranie prognoz i cen na horyzoncie planowania."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from energy_pricing import pricing_day_breakdown
from guardian_config import TELEMETRY_TZ
from load_forecast import forecast_load_hours
from planner.config import PLANNER_HORIZON_HOURS, PLANNER_LOAD_LOOKBACK_DAYS
from planner.models import HourInputs
from pv_forecast import fetch_hourly_pv_forecast

log = logging.getLogger("planner")


def _local_now() -> datetime:
    return datetime.now(ZoneInfo(TELEMETRY_TZ))


def _pv_kwh_from_row(row: dict) -> tuple[float, str]:
    kw = float(row.get("pv_kw") or 0.0)
    return kw, "solcast_proxy"


def build_hour_inputs(
    *,
    start_dt: datetime | None = None,
    hours: int | None = None,
) -> tuple[list[HourInputs], dict[str, Any]]:
    """
    Łączy prognozę load, PV i cennik na kolejne ``hours`` slotów od ``start_dt``.
    """
    now = start_dt or _local_now()
    horizon = hours if hours is not None else PLANNER_HORIZON_HOURS

    load_pack = forecast_load_hours(
        start_dt=now,
        hours=horizon,
        lookback_days=PLANNER_LOAD_LOOKBACK_DAYS,
    )
    load_by_key = {
        (r["date"], int(r["hour"])): r for r in load_pack.get("hours", [])
    }

    pv_pack: dict[str, Any] = {"hours": [], "error": None}
    try:
        pv_pack = fetch_hourly_pv_forecast(hours=max(horizon, 48))
    except Exception as e:
        log.warning("PV forecast unavailable: %s", e)
        pv_pack["error"] = str(e)

    pv_by_key: dict[tuple[str, int], dict] = {}
    for r in pv_pack.get("hours", []):
        pv_by_key[(str(r["date"]), int(r["hour"]))] = r

    out: list[HourInputs] = []
    pricing_cache: dict[str, dict] = {}

    for step in range(horizon):
        dt = now + timedelta(hours=step)
        d_iso = dt.date().isoformat()
        h = dt.hour
        key = (d_iso, h)

        lr = load_by_key.get(key, {})
        load_kwh = float(lr.get("load_kwh_p50") or 0.0)
        load_src = str(lr.get("source") or "unknown")

        pr = pv_by_key.get(key, {})
        pv_kwh, pv_src = _pv_kwh_from_row(pr) if pr else (0.0, "missing")

        if d_iso not in pricing_cache:
            pricing_cache[d_iso] = pricing_day_breakdown(dt.date())
        pb = pricing_cache[d_iso]
        ph = pb["hours"][h]
        imp = float(ph["import_pln_per_kwh"])
        rce = float(ph["rce_pln_kwh"])

        out.append(
            HourInputs(
                date=d_iso,
                hour=h,
                load_kwh=load_kwh,
                pv_kwh=pv_kwh,
                import_pln_per_kwh=imp,
                export_pln_per_kwh=rce,
                load_source=load_src,
                pv_source=pv_src,
            )
        )

    snapshot = {
        "generated_at": now.isoformat(),
        "timezone": TELEMETRY_TZ,
        "horizon_hours": horizon,
        "load_forecast": load_pack,
        "pv_forecast_meta": {
            k: pv_pack.get(k)
            for k in ("timezone", "source", "cached", "fetched_at", "error")
        },
        "pricing_dates": list(pricing_cache.keys()),
    }
    return out, snapshot


def latest_soc_from_telemetry(local_date: date | None = None) -> float | None:
    """Ostatni znany SOC z telemetrii (dziś lub wczoraj)."""
    from planner.telemetry import read_telemetry_day

    d = local_date or _local_now().date()
    for day in (d, d - timedelta(days=1)):
        rows = read_telemetry_day(day)
        if rows:
            try:
                return float(rows[-1].get("soc_pct"))
            except (TypeError, ValueError):
                pass
    return None
