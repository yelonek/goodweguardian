"""Piramida PV × RCE — prognoza na dziś+jutro (fakty + p50), tylko UX."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

import httpx

from energy_pricing import pricing_day_breakdown
from guardian_config import TELEMETRY_TZ
from load_forecast import build_daily_hourly_kwh_cache, forecast_load_hours, predict_load_one_hour
from pv_forecast import fetch_hourly_pv_forecast_with_history

if TYPE_CHECKING:
    from planner.models import DailyPlan

PV_PYRAMID_TIERS_GR: tuple[int, ...] = (10, 20, 30, 40, 50, 60)

PvSource = Literal["actual", "forecast", "missing"]


def _price_by_hour(pricing: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    if not pricing:
        return {}
    return {int(h["hour"]): h for h in pricing.get("hours", [])}


def _pv_forecast_kwh(pv_row: dict[str, Any] | None) -> float | None:
    if not pv_row:
        return None
    raw = pv_row.get("pv_kw")
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, v)


CHEAP_THRESHOLD_PLN = 0.59


def export_kwh_for_slot(
    d_iso: str,
    hour: int,
    *,
    pv_kwh: float,
    load_base_kwh: float,
    plan: DailyPlan | None,
) -> float:
    """Planowany eksport netto (planer) lub heurystyka PV − load_base."""
    if plan is not None:
        from planner.plan_store import hour_plan_from

        hp = hour_plan_from(plan, date.fromisoformat(d_iso), hour)
        if hp is not None:
            return max(0.0, float(hp.target_net_kwh))
    return max(0.0, pv_kwh - load_base_kwh)


def _load_base_kwh_for_hour(
    *,
    hour: int,
    hour_complete: bool,
    load_actual_map: dict[int, float],
    ev_map: dict[int, float],
    twc_on: bool,
    load_row: dict[str, Any] | None,
) -> float:
    if hour_complete:
        load_actual = load_actual_map.get(hour)
        if load_actual is not None:
            if twc_on and hour in ev_map:
                return max(0.0, float(load_actual) - float(ev_map[hour]))
            return float(load_actual)
    if load_row is not None:
        return float(
            load_row.get("load_base_kwh_p50") or load_row.get("load_kwh_p50") or 0.0
        )
    return 0.0


def _aggregate_pv_rce(
    hour_rows: list[dict[str, Any]],
    *,
    cheap_threshold_pln: float = CHEAP_THRESHOLD_PLN,
) -> dict[str, Any]:
    """Agreguj PV × RCE dla podzbioru godzin (segment dziś/jutro, było/zostało)."""
    threshold_pln = [g / 100.0 for g in PV_PYRAMID_TIERS_GR]

    pv_total = sum(float(r["pv_kwh"]) for r in hour_rows if r.get("pv_kwh") is not None)

    cumulative: list[float] = []
    for thr in threshold_pln:
        s = sum(
            float(r["pv_kwh"])
            for r in hour_rows
            if r.get("pv_kwh") is not None and float(r["rce_pln_kwh"]) < thr
        )
        cumulative.append(round(s, 4))

    above_60 = sum(
        float(r["pv_kwh"])
        for r in hour_rows
        if r.get("pv_kwh") is not None and float(r["rce_pln_kwh"]) >= cheap_threshold_pln
    )

    cheap_kwh = sum(
        float(r["pv_kwh"])
        for r in hour_rows
        if r.get("pv_kwh") is not None and float(r["rce_pln_kwh"]) < cheap_threshold_pln
    )

    cheap_surplus_kwh = sum(
        float(r["surplus_kwh"])
        for r in hour_rows
        if r.get("surplus_kwh") is not None
        and r.get("rce_pln_kwh") is not None
        and float(r["rce_pln_kwh"]) < cheap_threshold_pln
    )

    load_base_kwh = sum(
        float(r["load_base_kwh"])
        for r in hour_rows
        if r.get("load_base_kwh") is not None
        and r.get("rce_pln_kwh") is not None
        and float(r["rce_pln_kwh"]) < cheap_threshold_pln
    )

    tiers: list[dict[str, Any]] = []
    prev = 0.0
    for i, gr in enumerate(PV_PYRAMID_TIERS_GR):
        cum = cumulative[i]
        tiers.append(
            {
                "threshold_gr": gr,
                "cumulative_kwh": cum,
                "layer_kwh": round(max(0.0, cum - prev), 4),
            }
        )
        prev = cum

    hours_with_pv = sum(1 for r in hour_rows if r.get("pv_kwh") is not None)

    return {
        "pv_total_kwh": round(pv_total, 4),
        "cheap_kwh": round(cheap_kwh, 4),
        "cheap_surplus_kwh": round(cheap_surplus_kwh, 4),
        "load_base_kwh": round(load_base_kwh, 4),
        "above_60_kwh": round(above_60, 4),
        "tiers": tiers,
        "hours_with_pv": hours_with_pv,
    }


def build_pv_pyramid_payload(now: datetime | None = None) -> dict[str, Any]:
    """
    Horyzont 48 h od północy dziś (dziś + jutro).

    Godzina zakończona → PV z telemetrii (Δ E_pv); w trakcie / przyszła → prognoza p50.
    Progi RCE skumulowane (gr); osobno wiersz ≥ progu taniości.
    """
    from guardian_dashboard import (  # noqa: PLC0415 — unik circular import
        _pricing_for_day_quiet,
        _telemetry_hourly_load_pv_actuals,
    )
    from planner.plan_store import load_latest_plan
    from tesla_wall_charger import hourly_ev_kwh_from_telemetry, twc_enabled

    tz = ZoneInfo(TELEMETRY_TZ)
    now_local = (now or datetime.now(tz)).replace(tzinfo=None)
    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    lookback_days = 28
    cache_min = today - timedelta(days=lookback_days + 2)
    load_cache = build_daily_hourly_kwh_cache(min_date=cache_min)

    try:
        pricing_today = pricing_day_breakdown(today)
    except Exception:
        pricing_today = None
    pricing_tomorrow = _pricing_for_day_quiet(tomorrow)

    price_today = _price_by_hour(pricing_today)
    price_tomorrow = _price_by_hour(pricing_tomorrow)

    try:
        pv_payload = fetch_hourly_pv_forecast_with_history(hours_back=48, hours_forward=48)
    except (RuntimeError, httpx.HTTPError):
        pv_payload = {"hours": []}
    pv_by_dh = {
        (str(h.get("date")), int(h.get("hour"))): h for h in pv_payload.get("hours", [])
    }

    load_payload = forecast_load_hours(
        start_dt=now_local, hours=48, lookback_days=lookback_days, cache=load_cache
    )
    load_by_dh = {
        (str(h.get("date")), int(h.get("hour"))): h
        for h in load_payload.get("hours", [])
    }

    load_actual_today, pv_actual_today = _telemetry_hourly_load_pv_actuals(today)
    load_actual_tomorrow, pv_actual_tomorrow = _telemetry_hourly_load_pv_actuals(tomorrow)
    load_actual_by_date = {
        today.isoformat(): load_actual_today,
        tomorrow.isoformat(): load_actual_tomorrow,
    }
    pv_actual_by_date = {
        today.isoformat(): pv_actual_today,
        tomorrow.isoformat(): pv_actual_tomorrow,
    }

    twc_on = twc_enabled()
    ev_by_date: dict[str, dict[int, float]] = {}
    if twc_on:
        ev_by_date[today.isoformat()] = hourly_ev_kwh_from_telemetry(today)
        ev_by_date[tomorrow.isoformat()] = hourly_ev_kwh_from_telemetry(tomorrow)

    rolling_plan = load_latest_plan()

    warnings: list[str] = []
    hour_rows: list[dict[str, Any]] = []
    start_dt = datetime.combine(today, datetime.min.time())

    for offset in range(48):
        slot = start_dt + timedelta(hours=offset)
        d_iso = slot.date().isoformat()
        h = slot.hour
        slot_end = slot + timedelta(hours=1)
        hour_complete = slot_end <= now_local

        if slot.date() == today:
            p = price_today.get(h)
        elif slot.date() == tomorrow:
            p = price_tomorrow.get(h)
        else:
            p = None

        rce = p.get("rce_pln_kwh") if p else None
        if rce is None:
            warnings.append(f"brak RCE: {d_iso} h{h:02d}")
            hour_rows.append(
                {
                    "date": d_iso,
                    "hour": h,
                    "hour_complete": hour_complete,
                    "pv_kwh": None,
                    "pv_source": "missing",
                    "rce_pln_kwh": None,
                }
            )
            continue

        try:
            rce_f = float(rce)
        except (TypeError, ValueError):
            warnings.append(f"nieprawidłowe RCE: {d_iso} h{h:02d}")
            continue

        pv_actual_map = pv_actual_by_date.get(d_iso, {})
        pv_actual = pv_actual_map.get(h) if hour_complete else None
        pv_forecast = _pv_forecast_kwh(pv_by_dh.get((d_iso, h)))

        pv_kwh: float | None
        pv_source: PvSource
        if hour_complete and pv_actual is not None:
            pv_kwh = max(0.0, float(pv_actual))
            pv_source = "actual"
        elif pv_forecast is not None:
            pv_kwh = pv_forecast
            pv_source = "forecast"
        elif hour_complete and pv_actual is None:
            pv_kwh = pv_forecast
            pv_source = "forecast" if pv_forecast is not None else "missing"
            if pv_kwh is None:
                warnings.append(f"brak PV (fakt/prognoza): {d_iso} h{h:02d}")
        else:
            pv_kwh = None
            pv_source = "missing"
            warnings.append(f"brak prognozy PV: {d_iso} h{h:02d}")

        load_actual_map = load_actual_by_date.get(d_iso, {})
        ev_map = ev_by_date.get(d_iso, {})
        load_row = load_by_dh.get((d_iso, h))
        if load_row is None and not hour_complete:
            base = predict_load_one_hour(slot.date(), h, lookback_days, load_cache)
            load_row = base
        load_base_kwh = _load_base_kwh_for_hour(
            hour=h,
            hour_complete=hour_complete,
            load_actual_map=load_actual_map,
            ev_map=ev_map,
            twc_on=twc_on,
            load_row=load_row,
        )
        surplus_kwh = (
            export_kwh_for_slot(
                d_iso,
                h,
                pv_kwh=float(pv_kwh),
                load_base_kwh=load_base_kwh,
                plan=rolling_plan,
            )
            if pv_kwh is not None
            else None
        )

        hour_rows.append(
            {
                "date": d_iso,
                "hour": h,
                "hour_complete": hour_complete,
                "pv_kwh": pv_kwh,
                "pv_source": pv_source,
                "rce_pln_kwh": rce_f,
                "load_base_kwh": round(load_base_kwh, 4),
                "surplus_kwh": round(surplus_kwh, 4) if surplus_kwh is not None else None,
            }
        )

    today_iso = today.isoformat()
    tomorrow_iso = tomorrow.isoformat()

    today_past_rows = [
        r for r in hour_rows if r["date"] == today_iso and r.get("hour_complete")
    ]
    today_remaining_rows = [
        r for r in hour_rows if r["date"] == today_iso and not r.get("hour_complete")
    ]
    today_all_rows = [r for r in hour_rows if r["date"] == today_iso]
    tomorrow_rows = [r for r in hour_rows if r["date"] == tomorrow_iso]

    aggregate_all = _aggregate_pv_rce(hour_rows)
    segments = {
        "cheap_threshold_gr": int(CHEAP_THRESHOLD_PLN * 100),
        "today": {
            "date": today_iso,
            "past": _aggregate_pv_rce(today_past_rows),
            "remaining": _aggregate_pv_rce(today_remaining_rows),
            "total": _aggregate_pv_rce(today_all_rows),
        },
        "tomorrow": {
            "date": tomorrow_iso,
            "total": _aggregate_pv_rce(tomorrow_rows),
        },
    }

    hours_with_rce = sum(1 for r in hour_rows if r.get("rce_pln_kwh") is not None)

    return {
        "now": now_local.isoformat(timespec="seconds"),
        "timezone": TELEMETRY_TZ,
        "horizon_start": start_dt.isoformat(timespec="seconds"),
        "horizon_hours": 48,
        "pv_total_kwh": aggregate_all["pv_total_kwh"],
        "above_60_kwh": aggregate_all["above_60_kwh"],
        "tiers_gr": list(PV_PYRAMID_TIERS_GR),
        "tiers": aggregate_all["tiers"],
        "hours_with_pv": aggregate_all["hours_with_pv"],
        "hours_with_rce": hours_with_rce,
        "segments": segments,
        "pricing_today_source": pricing_today.get("source") if pricing_today else None,
        "pricing_tomorrow_available": pricing_tomorrow is not None,
        "pricing_tomorrow_source": pricing_tomorrow.get("source") if pricing_tomorrow else None,
        "warnings": sorted(set(warnings))[:12],
    }
