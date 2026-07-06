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
from planner.hour_remainder import scale_hour_inputs_for_remainder
from planner.pv_correction import apply_pv_correction
from pv_forecast import fetch_hourly_pv_forecast

log = logging.getLogger("planner")


def _local_now() -> datetime:
    return datetime.now(ZoneInfo(TELEMETRY_TZ))


def _pv_kwh_from_row(row: dict) -> tuple[float, str]:
    kw = float(row.get("pv_kw") or 0.0)
    return kw, "solcast_proxy"


def build_hour_inputs_for_slots(
    slots: list[tuple[str, int]],
    *,
    lookback_days: int | None = None,
    now: datetime | None = None,
) -> tuple[list[HourInputs], dict[str, Any]]:
    """Prognozy load/PV + cennik dla listy slotów ``(date_iso, hour)``."""
    if not slots:
        return [], {"slots": [], "timezone": TELEMETRY_TZ}

    lookback = lookback_days if lookback_days is not None else PLANNER_LOAD_LOOKBACK_DAYS
    first_d, first_h = slots[0]
    first_dt = datetime.fromisoformat(f"{first_d}T{first_h:02d}:00:00")
    n_slots = len(slots)

    load_pack = forecast_load_hours(
        start_dt=first_dt,
        hours=n_slots + 2,
        lookback_days=lookback,
    )
    load_by_key = {
        (r["date"], int(r["hour"])): r for r in load_pack.get("hours", [])
    }

    pv_pack: dict[str, Any] = {"hours": [], "error": None}
    try:
        pv_pack = fetch_hourly_pv_forecast(hours=max(n_slots + 2, 48))
    except Exception as e:
        log.warning("PV forecast unavailable: %s", e)
        pv_pack["error"] = str(e)

    pv_by_key: dict[tuple[str, int], dict] = {}
    for r in pv_pack.get("hours", []):
        pv_by_key[(str(r["date"]), int(r["hour"]))] = r

    now_local = now or _local_now().replace(tzinfo=None)
    pv_corrected, pv_sources, pv_correction_meta = apply_pv_correction(
        slots, pv_by_key, now=now_local
    )

    out: list[HourInputs] = []
    pricing_cache: dict[str, dict] = {}

    for d_iso, h in slots:
        key = (d_iso, h)
        lr = load_by_key.get(key, {})
        load_kwh = float(lr.get("load_kwh_p50") or 0.0)
        load_src = str(lr.get("source") or "unknown")

        if key in pv_corrected:
            pv_kwh = float(pv_corrected[key])
            pv_src = pv_sources.get(key, "solcast_proxy")
        else:
            pr = pv_by_key.get(key, {})
            pv_kwh, pv_src = _pv_kwh_from_row(pr) if pr else (0.0, "missing")

        pr = pv_by_key.get(key, {})
        pv_p50_raw = float(pr.get("pv_kw") or 0.0) if pr else pv_kwh
        pv_p10_raw = float(pr.get("pv_kw_p10") if pr and pr.get("pv_kw_p10") is not None else pv_p50_raw)
        pv_p90_raw = float(pr.get("pv_kw_p90") if pr and pr.get("pv_kw_p90") is not None else pv_p50_raw)
        if pv_p50_raw > 1e-9 and key in pv_corrected:
            k_scale = pv_kwh / pv_p50_raw
            pv_p10 = max(0.0, pv_p10_raw * k_scale)
            pv_p90 = max(0.0, pv_p90_raw * k_scale)
        else:
            pv_p10 = max(0.0, pv_p10_raw)
            pv_p90 = max(0.0, pv_p90_raw)

        load_p75 = float(lr.get("load_kwh_p75") if lr.get("load_kwh_p75") is not None else load_kwh)
        load_p25 = float(lr.get("load_kwh_p25") if lr.get("load_kwh_p25") is not None else load_kwh)

        if d_iso not in pricing_cache:
            pricing_cache[d_iso] = pricing_day_breakdown(date.fromisoformat(d_iso))
        pb = pricing_cache[d_iso]
        ph = pb["hours"][h]
        imp = float(ph["import_pln_per_kwh"])
        rce = float(ph["rce_pln_kwh"])

        hin = HourInputs(
            date=d_iso,
            hour=h,
            load_kwh=load_kwh,
            pv_kwh=pv_kwh,
            import_pln_per_kwh=imp,
            export_pln_per_kwh=rce,
            load_source=load_src,
            pv_source=pv_src,
            pv_kwh_p10=pv_p10,
            pv_kwh_p90=pv_p90,
            load_kwh_p75=load_p75,
            load_kwh_p25=load_p25,
        )
        out.append(
            scale_hour_inputs_for_remainder(
                hin, now=now_local, pv_correction_meta=pv_correction_meta
            )
        )

    snapshot = {
        "generated_at": first_dt.isoformat(),
        "timezone": TELEMETRY_TZ,
        "slots": [{"date": d, "hour": h} for d, h in slots],
        "load_forecast": load_pack,
        "pv_forecast_meta": {
            k: pv_pack.get(k)
            for k in ("timezone", "source", "cached", "fetched_at", "error")
        },
        "pv_correction": pv_correction_meta,
        "pricing_dates": list(pricing_cache.keys()),
    }
    return out, snapshot


def build_hour_inputs(
    *,
    start_dt: datetime | None = None,
    hours: int | None = None,
) -> tuple[list[HourInputs], dict[str, Any]]:
    """Kompatybilność wsteczna: kolejne ``hours`` slotów od ``start_dt``."""
    now = start_dt or _local_now().replace(tzinfo=None)
    horizon = hours if hours is not None else PLANNER_HORIZON_HOURS
    slots = [
        ((now + timedelta(hours=step)).date().isoformat(), (now + timedelta(hours=step)).hour)
        for step in range(horizon)
    ]
    return build_hour_inputs_for_slots(slots)


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
