"""Piramida PV × RCE — prognoza na dziś+jutro (fakty + p10/p50/p90), tylko UX."""

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


def _pv_kw_field(pv_row: dict[str, Any] | None, key: str) -> float | None:
    if not pv_row:
        return None
    raw = pv_row.get(key)
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, v)


def _pv_forecast_kwh(pv_row: dict[str, Any] | None) -> float | None:
    return _pv_kw_field(pv_row, "pv_kw")


def _pv_forecast_bands(
    pv_row: dict[str, Any] | None,
) -> tuple[float | None, float | None, float | None]:
    """Zwraca (p10, p50, p90) z Solcast; brak p10/p90 → kopia p50."""
    p50 = _pv_kw_field(pv_row, "pv_kw")
    if p50 is None:
        return None, None, None
    p10 = _pv_kw_field(pv_row, "pv_kw_p10")
    p90 = _pv_kw_field(pv_row, "pv_kw_p90")
    if p10 is None:
        p10 = p50
    if p90 is None:
        p90 = p50
    # Uporządkuj na wypadek dziwnych odpowiedzi API
    p10 = min(p10, p50)
    p90 = max(p90, p50)
    return p10, p50, p90


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


def surplus_bands_kwh(
    *,
    pv_p10: float,
    pv_p50: float,
    pv_p90: float,
    load_p25: float,
    load_p50: float,
    load_p75: float,
    surplus_p50: float,
) -> tuple[float, float, float]:
    """
    Nadwyżka p10/p50/p90 jak w scenariuszach planera:
    p10 (pesymistyczna) = PV p10 − load p75,
    p90 (optymistyczna) = PV p90 − load p25.
    p50 pochodzi z export_kwh_for_slot (plan lub PV p50 − load p50).
    """
    s10 = max(0.0, pv_p10 - load_p75)
    s90 = max(0.0, pv_p90 - load_p25)
    s50 = max(0.0, float(surplus_p50))
    # Zachowaj spójność pasm względem p50
    s10 = min(s10, s50)
    s90 = max(s90, s50)
    return s10, s50, s90


def _load_base_kwh_for_hour(
    *,
    hour: int,
    hour_complete: bool,
    load_actual_map: dict[int, float],
    ev_map: dict[int, float],
    twc_on: bool,
    load_row: dict[str, Any] | None,
) -> float:
    bands = _load_base_bands_for_hour(
        hour=hour,
        hour_complete=hour_complete,
        load_actual_map=load_actual_map,
        ev_map=ev_map,
        twc_on=twc_on,
        load_row=load_row,
    )
    return bands[1]


def _load_base_bands_for_hour(
    *,
    hour: int,
    hour_complete: bool,
    load_actual_map: dict[int, float],
    ev_map: dict[int, float],
    twc_on: bool,
    load_row: dict[str, Any] | None,
) -> tuple[float, float, float]:
    """Zwraca (p25, p50, p75). Fakt → wszystkie trzy = load_base faktyczny."""
    if hour_complete:
        load_actual = load_actual_map.get(hour)
        if load_actual is not None:
            if twc_on and hour in ev_map:
                actual = max(0.0, float(load_actual) - float(ev_map[hour]))
            else:
                actual = float(load_actual)
            return actual, actual, actual
    if load_row is not None:
        p50 = float(
            load_row.get("load_base_kwh_p50") or load_row.get("load_kwh_p50") or 0.0
        )
        p25 = float(
            load_row.get("load_base_kwh_p25")
            or load_row.get("load_kwh_p25")
            or p50
        )
        p75 = float(
            load_row.get("load_base_kwh_p75")
            or load_row.get("load_kwh_p75")
            or p50
        )
        p25 = min(p25, p50)
        p75 = max(p75, p50)
        return max(0.0, p25), max(0.0, p50), max(0.0, p75)
    return 0.0, 0.0, 0.0


def _sum_band(hour_rows: list[dict[str, Any]], key: str, *, cheap_only: bool, cheap_threshold_pln: float) -> float:
    total = 0.0
    for r in hour_rows:
        if r.get(key) is None:
            continue
        if cheap_only:
            if r.get("rce_pln_kwh") is None:
                continue
            if float(r["rce_pln_kwh"]) >= cheap_threshold_pln:
                continue
        total += float(r[key])
    return total


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

    cheap_kwh_p10 = _sum_band(hour_rows, "pv_kwh_p10", cheap_only=True, cheap_threshold_pln=cheap_threshold_pln)
    cheap_kwh_p50 = _sum_band(hour_rows, "pv_kwh", cheap_only=True, cheap_threshold_pln=cheap_threshold_pln)
    cheap_kwh_p90 = _sum_band(hour_rows, "pv_kwh_p90", cheap_only=True, cheap_threshold_pln=cheap_threshold_pln)

    # Fallback: stare wiersze bez pasm → p50
    if not any(r.get("pv_kwh_p10") is not None for r in hour_rows):
        cheap_kwh_p10 = cheap_kwh_p50
    if not any(r.get("pv_kwh_p90") is not None for r in hour_rows):
        cheap_kwh_p90 = cheap_kwh_p50

    cheap_surplus_p10 = _sum_band(
        hour_rows, "surplus_kwh_p10", cheap_only=True, cheap_threshold_pln=cheap_threshold_pln
    )
    cheap_surplus_p50 = _sum_band(
        hour_rows, "surplus_kwh", cheap_only=True, cheap_threshold_pln=cheap_threshold_pln
    )
    cheap_surplus_p90 = _sum_band(
        hour_rows, "surplus_kwh_p90", cheap_only=True, cheap_threshold_pln=cheap_threshold_pln
    )
    if not any(r.get("surplus_kwh_p10") is not None for r in hour_rows):
        cheap_surplus_p10 = cheap_surplus_p50
    if not any(r.get("surplus_kwh_p90") is not None for r in hour_rows):
        cheap_surplus_p90 = cheap_surplus_p50

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
        "cheap_kwh": round(cheap_kwh_p50, 4),
        "cheap_kwh_p10": round(cheap_kwh_p10, 4),
        "cheap_kwh_p50": round(cheap_kwh_p50, 4),
        "cheap_kwh_p90": round(cheap_kwh_p90, 4),
        "cheap_surplus_kwh": round(cheap_surplus_p50, 4),
        "cheap_surplus_kwh_p10": round(cheap_surplus_p10, 4),
        "cheap_surplus_kwh_p50": round(cheap_surplus_p50, 4),
        "cheap_surplus_kwh_p90": round(cheap_surplus_p90, 4),
        "load_base_kwh": round(load_base_kwh, 4),
        "above_60_kwh": round(above_60, 4),
        "tiers": tiers,
        "hours_with_pv": hours_with_pv,
    }


def build_pv_pyramid_payload(now: datetime | None = None) -> dict[str, Any]:
    """
    Horyzont 48 h od północy dziś (dziś + jutro).

    Godzina zakończona → PV z telemetrii (Δ E_pv); w trakcie / przyszła → prognoza p10/p50/p90.
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
                    "pv_kwh_p10": None,
                    "pv_kwh_p90": None,
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
        pv_p10_f, pv_p50_f, pv_p90_f = _pv_forecast_bands(pv_by_dh.get((d_iso, h)))

        pv_kwh: float | None
        pv_kwh_p10: float | None
        pv_kwh_p90: float | None
        pv_source: PvSource
        if hour_complete and pv_actual is not None:
            actual = max(0.0, float(pv_actual))
            pv_kwh = actual
            pv_kwh_p10 = actual
            pv_kwh_p90 = actual
            pv_source = "actual"
        elif pv_p50_f is not None:
            pv_kwh = pv_p50_f
            pv_kwh_p10 = pv_p10_f
            pv_kwh_p90 = pv_p90_f
            pv_source = "forecast"
        elif hour_complete and pv_actual is None:
            pv_kwh = pv_p50_f
            pv_kwh_p10 = pv_p10_f
            pv_kwh_p90 = pv_p90_f
            pv_source = "forecast" if pv_p50_f is not None else "missing"
            if pv_kwh is None:
                warnings.append(f"brak PV (fakt/prognoza): {d_iso} h{h:02d}")
        else:
            pv_kwh = None
            pv_kwh_p10 = None
            pv_kwh_p90 = None
            pv_source = "missing"
            warnings.append(f"brak prognozy PV: {d_iso} h{h:02d}")

        load_actual_map = load_actual_by_date.get(d_iso, {})
        ev_map = ev_by_date.get(d_iso, {})
        load_row = load_by_dh.get((d_iso, h))
        if load_row is None and not hour_complete:
            base = predict_load_one_hour(slot.date(), h, lookback_days, load_cache)
            load_row = base
        load_p25, load_p50, load_p75 = _load_base_bands_for_hour(
            hour=h,
            hour_complete=hour_complete,
            load_actual_map=load_actual_map,
            ev_map=ev_map,
            twc_on=twc_on,
            load_row=load_row,
        )
        load_base_kwh = load_p50

        surplus_kwh: float | None = None
        surplus_p10: float | None = None
        surplus_p90: float | None = None
        if pv_kwh is not None and pv_kwh_p10 is not None and pv_kwh_p90 is not None:
            surplus_mid = export_kwh_for_slot(
                d_iso,
                h,
                pv_kwh=float(pv_kwh),
                load_base_kwh=load_base_kwh,
                plan=rolling_plan,
            )
            s10, s50, s90 = surplus_bands_kwh(
                pv_p10=float(pv_kwh_p10),
                pv_p50=float(pv_kwh),
                pv_p90=float(pv_kwh_p90),
                load_p25=load_p25,
                load_p50=load_p50,
                load_p75=load_p75,
                surplus_p50=surplus_mid,
            )
            surplus_kwh = s50
            surplus_p10 = s10
            surplus_p90 = s90

        hour_rows.append(
            {
                "date": d_iso,
                "hour": h,
                "hour_complete": hour_complete,
                "pv_kwh": pv_kwh,
                "pv_kwh_p10": pv_kwh_p10,
                "pv_kwh_p90": pv_kwh_p90,
                "pv_source": pv_source,
                "rce_pln_kwh": rce_f,
                "load_base_kwh": round(load_base_kwh, 4),
                "load_base_kwh_p25": round(load_p25, 4),
                "load_base_kwh_p75": round(load_p75, 4),
                "surplus_kwh": round(surplus_kwh, 4) if surplus_kwh is not None else None,
                "surplus_kwh_p10": round(surplus_p10, 4) if surplus_p10 is not None else None,
                "surplus_kwh_p90": round(surplus_p90, 4) if surplus_p90 is not None else None,
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
