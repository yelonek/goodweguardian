"""Eksperymentalna korekta PV pogodą OWM (Tier1) na horyzoncie h+2…h+6.

Źródło: darmowe Current Weather + 5-day/3h forecast (nie One Call).
Pola: ``clouds``, ``pop``, ``weather.id``, ``rain``/``snow`` (3h→≈1h).
``uvi`` / minutely — niedostępne w Free; clearness polega na clouds/pop/weather.

Solcast już zawiera chmury — ``k_wx`` jest **względne** do referencyjnego
clearness (nie mnożymy surowego zachmurzenia drugi raz od zera).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from weather_owm import (
    current_tier1,
    fetch_weather_pack,
    hourly_by_local_slot,
    minutely_mean_precip_mmh,
    owm_configured,
)

log = logging.getLogger("planner")

HorizonSlot = tuple[str, int]

# Horyzont korekty względem bieżącej godziny lokalnej (włącznie).
PV_WEATHER_HORIZON_START_H = 2
PV_WEATHER_HORIZON_END_H = 6

# Clearness referencyjny ≈ umiarkowane zachmurzenie, bez opadu.
PV_WEATHER_CLEARNESS_REF = 0.55
PV_WEATHER_K_MIN = 0.45
PV_WEATHER_K_MAX = 1.25

# Minutely: średnie mm/h powyżej progu → lekki derate na najbliższych slotach wx.
PV_WEATHER_MINUTELY_PRECIP_MMH = 0.15
PV_WEATHER_MINUTELY_FACTOR = 0.85


def pv_weather_correction_enabled() -> bool:
    """Przełącznik z Ustawień (``state/settings.json``), nie z ``.env``."""
    from guardian_settings import get_settings

    return bool(get_settings().pv_weather_correction_enabled)


def clip_k(value: float, *, k_min: float, k_max: float) -> float:
    return max(k_min, min(k_max, value))


def weather_id_penalty(weather_id: int | None) -> float:
    """Mnożnik warunków synoptycznych (OWM weather condition id)."""
    if weather_id is None:
        return 1.0
    wid = int(weather_id)
    if 200 <= wid < 300:
        return 0.45
    if 300 <= wid < 400:
        return 0.75
    if 500 <= wid < 600:
        return 0.55 if wid >= 520 else 0.65
    if 600 <= wid < 700:
        return 0.50
    if 700 <= wid < 800:
        # mgła / zamglenie / dym / pył
        if wid in (701, 711, 721, 741, 761):
            return 0.60
        return 0.80
    return 1.0


def clearness_proxy(
    *,
    clouds: float,
    uvi: float | None,
    pop: float | None,
    weather_id: int | None,
    rain_1h: float = 0.0,
    snow_1h: float = 0.0,
) -> float:
    """
    Proxy przejaśnienia 0…~1.2 z pól Tier1.

    ``clouds``: overcast nadal przepuszcza ~22% (dyfuzja).
    ``uvi``: moduluje tylko gdy > 0 (noc → polegamy na clouds/weather).
    """
    c = max(0.0, min(100.0, float(clouds)))
    cloud_t = 1.0 - 0.78 * (c / 100.0)

    if uvi is not None and float(uvi) > 0.05:
        uvi_f = clip_k(float(uvi) / 8.0, k_min=0.35, k_max=1.15)
    else:
        uvi_f = 1.0

    p = max(0.0, min(1.0, float(pop or 0.0)))
    pop_f = 1.0 - 0.40 * p

    precip_mm = max(0.0, float(rain_1h) + float(snow_1h))
    if precip_mm > 0.0:
        rain_f = clip_k(1.0 - 0.15 * min(precip_mm, 5.0), k_min=0.40, k_max=1.0)
    else:
        rain_f = 1.0

    return max(0.0, cloud_t * uvi_f * pop_f * rain_f * weather_id_penalty(weather_id))


def k_wx_from_tier1(
    *,
    clouds: float,
    uvi: float | None,
    pop: float | None,
    weather_id: int | None,
    rain_1h: float = 0.0,
    snow_1h: float = 0.0,
    clearness_ref: float = PV_WEATHER_CLEARNESS_REF,
    k_min: float = PV_WEATHER_K_MIN,
    k_max: float = PV_WEATHER_K_MAX,
    minutely_mean_mmh: float | None = None,
    apply_minutely: bool = False,
) -> tuple[float, dict[str, Any]]:
    """``k_wx = clip(clearness / ref)``; opcjonalnie minutely derate."""
    clearness = clearness_proxy(
        clouds=clouds,
        uvi=uvi,
        pop=pop,
        weather_id=weather_id,
        rain_1h=rain_1h,
        snow_1h=snow_1h,
    )
    ref = max(1e-6, float(clearness_ref))
    k_raw = clearness / ref
    k = clip_k(k_raw, k_min=k_min, k_max=k_max)
    minutely_applied = False
    if (
        apply_minutely
        and minutely_mean_mmh is not None
        and float(minutely_mean_mmh) >= PV_WEATHER_MINUTELY_PRECIP_MMH
    ):
        k = clip_k(k * PV_WEATHER_MINUTELY_FACTOR, k_min=k_min, k_max=k_max)
        minutely_applied = True
    meta = {
        "clearness": clearness,
        "clearness_ref": ref,
        "k_raw": k_raw,
        "k_wx": k,
        "clouds": float(clouds),
        "uvi": uvi,
        "pop": pop,
        "weather_id": weather_id,
        "rain_1h": rain_1h,
        "snow_1h": snow_1h,
        "minutely_mean_mmh": minutely_mean_mmh,
        "minutely_applied": minutely_applied,
    }
    return k, meta


def _hour_offset(slot: HorizonSlot, now: datetime) -> int:
    slot_dt = datetime.fromisoformat(f"{slot[0]}T{slot[1]:02d}:00:00")
    base = now.replace(minute=0, second=0, microsecond=0)
    return int((slot_dt - base).total_seconds() // 3600)


def apply_pv_weather_correction(
    slots: list[HorizonSlot],
    pv_by_key: dict[HorizonSlot, dict],
    corrected: dict[HorizonSlot, float],
    sources: dict[HorizonSlot, str],
    *,
    now: datetime,
    weather_pack: dict[str, Any] | None = None,
    onecall: dict[str, Any] | None = None,
    enabled: bool | None = None,
) -> tuple[dict[HorizonSlot, float], dict[HorizonSlot, str], dict[str, Any]]:
    """
    Skaluje Solcast (lub już skorygowane wartości) na slotach h+2…h+6 przez ``k_wx``.

    Nie rusza slotów z ``pv_intra_*`` (bieżąca / h+1 zostają przy ``k_intra``).
    ``onecall`` — alias wsteczny dla ``weather_pack``.
    """
    if enabled is None:
        enabled = pv_weather_correction_enabled()

    meta: dict[str, Any] = {
        "enabled": bool(enabled),
        "applied": False,
        "tier": 1,
        "api": "openweathermap_free_2_5",
        "horizon_start_h": PV_WEATHER_HORIZON_START_H,
        "horizon_end_h": PV_WEATHER_HORIZON_END_H,
        "reason": "disabled",
        "owm_meta": None,
        "current": None,
        "minutely_mean_mmh": None,
        "slots_adjusted": [],
    }

    out_corr = dict(corrected)
    out_src = dict(sources)

    if not enabled:
        return out_corr, out_src, meta

    pack_arg = weather_pack if weather_pack is not None else onecall
    if not owm_configured() and pack_arg is None:
        meta["reason"] = "not_configured"
        return out_corr, out_src, meta

    if not slots:
        meta["reason"] = "no_slots"
        return out_corr, out_src, meta

    try:
        pack = pack_arg if pack_arg is not None else fetch_weather_pack()
    except Exception as e:
        log.warning("PV weather: OWM fetch error: %s", e)
        meta["reason"] = f"fetch_error:{e}"
        return out_corr, out_src, meta

    owm_meta = pack.get("_meta") if isinstance(pack, dict) else None
    meta["owm_meta"] = owm_meta
    if isinstance(owm_meta, dict) and owm_meta.get("error") and not pack.get("hourly"):
        meta["reason"] = str(owm_meta.get("error"))
        return out_corr, out_src, meta

    by_hour = hourly_by_local_slot(pack)
    minutely_mean = minutely_mean_precip_mmh(pack)
    meta["minutely_mean_mmh"] = minutely_mean
    meta["current"] = current_tier1(pack)

    if not by_hour:
        meta["reason"] = "no_hourly"
        return out_corr, out_src, meta

    adjusted: list[dict[str, Any]] = []
    for slot in slots:
        src = out_src.get(slot, "solcast_proxy")
        if src.startswith("pv_intra"):
            continue
        offset = _hour_offset(slot, now)
        if offset < PV_WEATHER_HORIZON_START_H or offset > PV_WEATHER_HORIZON_END_H:
            continue

        wx = by_hour.get(slot)
        if wx is None:
            continue

        # Minutely niedostępne w Free; flaga zostaje dla kompatybilności testów.
        apply_minutely = offset == PV_WEATHER_HORIZON_START_H
        k, k_meta = k_wx_from_tier1(
            clouds=float(wx["clouds"]),
            uvi=wx.get("uvi"),
            pop=wx.get("pop"),
            weather_id=wx.get("weather_id"),
            rain_1h=float(wx.get("rain_1h") or 0.0),
            snow_1h=float(wx.get("snow_1h") or 0.0),
            minutely_mean_mmh=minutely_mean,
            apply_minutely=apply_minutely,
        )

        base = float(out_corr.get(slot, pv_by_key.get(slot, {}).get("pv_kw") or 0.0))
        value = max(0.0, base * k)
        out_corr[slot] = value
        out_src[slot] = "pv_weather_tier1"
        adjusted.append(
            {
                "date": slot[0],
                "hour": slot[1],
                "hour_offset": offset,
                "pv_kwh_before": base,
                "pv_kwh": value,
                "k_wx": k,
                **{
                    kk: k_meta[kk]
                    for kk in (
                        "clearness",
                        "clouds",
                        "uvi",
                        "pop",
                        "weather_id",
                        "minutely_applied",
                    )
                },
            }
        )

    meta["slots_adjusted"] = adjusted
    meta["applied"] = bool(adjusted)
    meta["reason"] = "ok" if adjusted else "no_matching_hourly"
    return out_corr, out_src, meta


def build_pv_weather_dashboard(
    slots: list[HorizonSlot],
    pv_by_key: dict[HorizonSlot, dict],
    corrected_intra: dict[HorizonSlot, float],
    sources_intra: dict[HorizonSlot, str],
    *,
    now: datetime,
    weather_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Snapshot do panelu dashboardu: zawsze liczy podgląd ``k_wx`` (gdy OWM skonfigurowane),
    a ``affects_plan`` zależy od przełącznika w Ustawieniach.
    """
    settings_on = pv_weather_correction_enabled()
    configured = owm_configured() or weather_pack is not None

    base: dict[str, Any] = {
        "enabled": settings_on,
        "configured": bool(owm_configured()),
        "affects_plan": False,
        "tier": 1,
        "api": "openweathermap_free_2_5",
        "horizon_start_h": PV_WEATHER_HORIZON_START_H,
        "horizon_end_h": PV_WEATHER_HORIZON_END_H,
        "reason": "disabled",
        "owm_meta": None,
        "current": None,
        "hours": [],
        "delta_kwh_sum": None,
    }

    if not configured and weather_pack is None:
        base["reason"] = "not_configured"
        return base

    # Podgląd zawsze z enabled=True — UI pokazuje wpływ nawet gdy przełącznik off.
    _corr, _src, preview = apply_pv_weather_correction(
        slots,
        pv_by_key,
        corrected_intra,
        sources_intra,
        now=now,
        weather_pack=weather_pack,
        enabled=True,
    )

    hours: list[dict[str, Any]] = []
    delta_sum = 0.0
    for row in preview.get("slots_adjusted") or []:
        before = float(row.get("pv_kwh_before") or 0.0)
        after = float(row.get("pv_kwh") or 0.0)
        delta = after - before
        delta_sum += delta
        hours.append(
            {
                "date": row.get("date"),
                "hour": row.get("hour"),
                "hour_offset": row.get("hour_offset"),
                "solcast_kwh": before,
                "wx_kwh": after,
                "delta_kwh": delta,
                "k_wx": row.get("k_wx"),
                "clearness": row.get("clearness"),
                "clouds": row.get("clouds"),
                "uvi": row.get("uvi"),
                "pop": row.get("pop"),
                "weather_id": row.get("weather_id"),
                "affects_plan": bool(settings_on and preview.get("applied")),
            }
        )

    reason = str(preview.get("reason") or "ok")
    if not settings_on:
        reason = "disabled_in_settings" if reason in ("ok", "disabled") else reason

    base.update(
        {
            "affects_plan": bool(settings_on and preview.get("applied")),
            "reason": reason,
            "owm_meta": preview.get("owm_meta"),
            "current": preview.get("current"),
            "hours": hours,
            "delta_kwh_sum": delta_sum if hours else None,
            "preview_applied": bool(preview.get("applied")),
        }
    )
    return base
