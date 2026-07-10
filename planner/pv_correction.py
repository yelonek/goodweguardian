"""Korekta prognozy PV: k_intra na bieżącą godzinę i h+1."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from guardian_config import (
    PV_CORRECTION_CLIP_RAMP_END,
    PV_CORRECTION_CLIP_RAMP_START,
    PV_CORRECTION_DYNAMIC_CLIP_ENABLED,
    PV_CORRECTION_ENABLED,
    PV_CORRECTION_EPS_KWH,
    PV_CORRECTION_K_MAX,
    PV_CORRECTION_K_MAX_WIDE,
    PV_CORRECTION_K_MIN,
    PV_CORRECTION_K_MIN_WIDE,
    PV_CORRECTION_RATE_BLEND_END,
    PV_CORRECTION_RATE_BLEND_START,
    PV_CORRECTION_RATE_ENABLED,
    PV_CORRECTION_RATE_WINDOW_MIN,
    TELEMETRY_DIR,
)

log = logging.getLogger("planner")

HorizonSlot = tuple[str, int]


def hour_elapsed_fraction(now: datetime) -> float:
    """α = ułamek bieżącej godziny lokalnej (0 na :00:00)."""
    return (now.minute + now.second / 60.0) / 60.0


def clip_k(value: float, *, k_min: float, k_max: float) -> float:
    return max(k_min, min(k_max, value))


def _rate_blend_weight(
    alpha: float,
    *,
    blend_start: float = PV_CORRECTION_RATE_BLEND_START,
    blend_end: float = PV_CORRECTION_RATE_BLEND_END,
) -> float:
    """0 na początku godziny → 1 gdy alpha >= blend_end."""
    if blend_end <= blend_start:
        return 0.0
    if alpha <= blend_start:
        return 0.0
    if alpha >= blend_end:
        return 1.0
    return (alpha - blend_start) / (blend_end - blend_start)


def pv_recent_average_kw(
    now: datetime,
    *,
    window_min: int = PV_CORRECTION_RATE_WINDOW_MIN,
) -> tuple[float, int] | None:
    """
    Średnia moc PV [kW] z ostatnich ``window_min`` minut bieżącej godziny lokalnej.
    """
    if window_min <= 0:
        return None
    path = TELEMETRY_DIR / f"telemetry_{now.date().isoformat()}.jsonl"
    target_hour = now.hour
    start_minute = max(0, now.minute - window_min)
    power_kw: list[float] = []

    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if int(row["local_hour"]) != target_hour:
                        continue
                    minute = int(row.get("local_minute", 0))
                    if minute > now.minute or minute < start_minute:
                        continue
                    power_kw.append(float(row.get("pv_w", 0.0)) / 1000.0)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
    except OSError as e:
        log.debug("pv recent average read failed %s: %s", path, e)
        return None

    if not power_kw:
        return None
    return sum(power_kw) / len(power_kw), len(power_kw)


def pv_energy_so_far_in_hour(now: datetime) -> tuple[float, int] | None:
    """
    Energia PV [kWh] od początku bieżącej godziny lokalnej (średnia moc × czas próbek).

    Zwraca (energia, liczba_próbek) lub None gdy brak telemetrii w tej godzinie.
    """
    path = TELEMETRY_DIR / f"telemetry_{now.date().isoformat()}.jsonl"
    target_hour = now.hour
    energy_kwh = 0.0
    count = 0

    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if int(row["local_hour"]) != target_hour:
                        continue
                    minute = int(row.get("local_minute", 0))
                    if minute > now.minute:
                        continue
                    energy_kwh += float(row.get("pv_w", 0.0)) / 1000.0 / 60.0
                    count += 1
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
    except OSError as e:
        log.debug("pv energy read failed %s: %s", path, e)
        return None

    if count == 0:
        return None
    return energy_kwh, count


def _clip_ramp_weight(
    alpha: float,
    *,
    ramp_start: float = PV_CORRECTION_CLIP_RAMP_START,
    ramp_end: float = PV_CORRECTION_CLIP_RAMP_END,
) -> float:
    """0 = wąski clip (początek h), 1 = szeroki clip (późna godzina)."""
    if ramp_end <= ramp_start:
        return 0.0
    if alpha <= ramp_start:
        return 0.0
    if alpha >= ramp_end:
        return 1.0
    return (alpha - ramp_start) / (ramp_end - ramp_start)


def effective_clip_bounds(
    alpha: float,
    *,
    k_min: float = PV_CORRECTION_K_MIN,
    k_max: float = PV_CORRECTION_K_MAX,
    k_min_wide: float = PV_CORRECTION_K_MIN_WIDE,
    k_max_wide: float = PV_CORRECTION_K_MAX_WIDE,
    dynamic_enabled: bool | None = None,
) -> tuple[float, float, float]:
    """
    Efektywne granice clipu k_intra.

    Zwraca (k_min_eff, k_max_eff, dynamic_weight).
    """
    if dynamic_enabled is None:
        dynamic_enabled = PV_CORRECTION_DYNAMIC_CLIP_ENABLED
    if not dynamic_enabled:
        return k_min, k_max, 0.0
    w = _clip_ramp_weight(alpha)
    k_min_eff = (1.0 - w) * k_min + w * k_min_wide
    k_max_eff = (1.0 - w) * k_max + w * k_max_wide
    return k_min_eff, k_max_eff, w


def pv_minute_series_in_hour(now: datetime) -> list[dict[str, float | int]]:
    """Minutowa seria PV w bieżącej godzinie: moc [kW] i energia skumulowana [kWh]."""
    path = TELEMETRY_DIR / f"telemetry_{now.date().isoformat()}.jsonl"
    target_hour = now.hour
    if not path.exists():
        return []

    by_minute: dict[int, float] = {}
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if int(row["local_hour"]) != target_hour:
                        continue
                    minute = int(row.get("local_minute", 0))
                    if minute > now.minute:
                        continue
                    by_minute[minute] = float(row.get("pv_w", 0.0)) / 1000.0
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
    except OSError as e:
        log.debug("pv minute series read failed %s: %s", path, e)
        return []

    if not by_minute:
        return []

    series: list[dict[str, float | int]] = []
    cum_kwh = 0.0
    for minute in sorted(by_minute):
        pv_kw = by_minute[minute]
        cum_kwh += pv_kw / 60.0
        series.append({"minute": minute, "pv_kw": pv_kw, "cum_kwh": cum_kwh})
    return series


def compute_k_intra_detail(
    *,
    f50_kwh: float,
    a_so_far_kwh: float,
    alpha: float,
    eps_kwh_per_h: float = PV_CORRECTION_EPS_KWH,
    k_min: float = PV_CORRECTION_K_MIN,
    k_max: float = PV_CORRECTION_K_MAX,
) -> tuple[float | None, str, dict[str, Any]]:
    """
    k_intra z metadanymi (k_raw, dynamic clip, F_elapsed).

    Gdy F_elapsed <= ε×α — brak sensownego stosunku (noc, początek godziny).
    """
    meta: dict[str, Any] = {
        "k_raw": None,
        "k_intra": None,
        "clip_min_base": k_min,
        "clip_max_base": k_max,
        "clip_min_wide": PV_CORRECTION_K_MIN_WIDE,
        "clip_max_wide": PV_CORRECTION_K_MAX_WIDE,
        "clip_min_effective": k_min,
        "clip_max_effective": k_max,
        "dynamic_clip_weight": 0.0,
        "dynamic_clip_enabled": PV_CORRECTION_DYNAMIC_CLIP_ENABLED,
        "f_elapsed_kwh": alpha * f50_kwh if alpha > 0 else 0.0,
    }
    if alpha <= 0.0:
        return None, "hour_start", meta
    f_elapsed = alpha * f50_kwh
    meta["f_elapsed_kwh"] = f_elapsed
    if f_elapsed <= eps_kwh_per_h * alpha:
        return None, "f_elapsed_below_eps", meta

    k_raw = a_so_far_kwh / f_elapsed
    k_min_eff, k_max_eff, w = effective_clip_bounds(alpha, k_min=k_min, k_max=k_max)
    k_intra = clip_k(k_raw, k_min=k_min_eff, k_max=k_max_eff)
    meta.update(
        {
            "k_raw": k_raw,
            "k_intra": k_intra,
            "clip_min_effective": k_min_eff,
            "clip_max_effective": k_max_eff,
            "dynamic_clip_weight": w,
        }
    )
    return k_intra, "ok", meta


def compute_k_intra(
    *,
    f50_kwh: float,
    a_so_far_kwh: float,
    alpha: float,
    eps_kwh_per_h: float = PV_CORRECTION_EPS_KWH,
    k_min: float = PV_CORRECTION_K_MIN,
    k_max: float = PV_CORRECTION_K_MAX,
) -> tuple[float | None, str]:
    k_intra, reason, _ = compute_k_intra_detail(
        f50_kwh=f50_kwh,
        a_so_far_kwh=a_so_far_kwh,
        alpha=alpha,
        eps_kwh_per_h=eps_kwh_per_h,
        k_min=k_min,
        k_max=k_max,
    )
    return k_intra, reason


def pv_plan_current_hour_kwh(
    *,
    f50_kwh: float,
    a_so_far_kwh: float,
    alpha: float,
    k_intra: float,
    recent_kw: float | None = None,
    rate_enabled: bool = PV_CORRECTION_RATE_ENABLED,
) -> tuple[float, dict[str, Any]]:
    """
    Prognoza na pełną bieżącą godzinę [kWh/h].

    Bazowo: ``A_so_far + (1−α) × F50 × k_intra``.
    Opcjonalnie blend z estymatą rate: ``A_so_far + recent_kw × (1−α)``
    (waga rate rośnie z α — lepiej łapie nagłe chmury w środku slotu).
    """
    remaining = (1.0 - alpha) * f50_kwh * k_intra
    k_plan = max(0.0, a_so_far_kwh + remaining)
    meta: dict[str, Any] = {
        "method": "k_intra",
        "k_plan_kwh": k_plan,
        "rate_plan_kwh": None,
        "rate_blend_weight": 0.0,
        "recent_kw": recent_kw,
    }

    if not rate_enabled or recent_kw is None or alpha <= 0.0:
        return max(a_so_far_kwh, k_plan), meta

    rate_plan = max(0.0, a_so_far_kwh + float(recent_kw) * (1.0 - alpha))
    weight = _rate_blend_weight(alpha)
    blended = (1.0 - weight) * k_plan + weight * rate_plan
    meta.update(
        {
            "method": "k_intra_rate_blend" if weight > 0.0 else "k_intra",
            "rate_plan_kwh": rate_plan,
            "rate_blend_weight": weight,
        }
    )
    return max(a_so_far_kwh, blended), meta


def pv_plan_next_hour_kwh(*, f50_kwh: float, k_intra: float) -> float:
    return max(0.0, f50_kwh * k_intra)


def build_pv_intra_state(
    now: datetime,
    *,
    f50_current_kwh: float,
) -> dict[str, Any]:
    """Stan korekty dla bieżącej godziny lokalnej."""
    alpha = hour_elapsed_fraction(now)
    energy = pv_energy_so_far_in_hour(now)
    a_so_far = float(energy[0]) if energy is not None else None
    samples = int(energy[1]) if energy is not None else 0
    recent = pv_recent_average_kw(now)
    recent_kw = float(recent[0]) if recent is not None else None
    recent_samples = int(recent[1]) if recent is not None else 0

    meta: dict[str, Any] = {
        "enabled": PV_CORRECTION_ENABLED,
        "applied": False,
        "alpha": alpha,
        "f50_current_kwh": f50_current_kwh,
        "a_so_far_kwh": a_so_far,
        "telemetry_samples": samples,
        "recent_kw": recent_kw,
        "recent_samples": recent_samples,
        "f_elapsed_kwh": alpha * f50_current_kwh,
        "k_intra": None,
        "reason": "disabled",
        "plan_method": None,
    }

    if not PV_CORRECTION_ENABLED:
        return meta

    if a_so_far is None:
        meta["reason"] = "no_telemetry"
        return meta

    k_intra, reason, k_detail = compute_k_intra_detail(
        f50_kwh=f50_current_kwh,
        a_so_far_kwh=a_so_far,
        alpha=alpha,
    )
    meta.update(k_detail)
    meta["k_intra"] = k_intra
    meta["reason"] = reason
    meta["applied"] = k_intra is not None
    return meta


def apply_pv_correction(
    slots: list[HorizonSlot],
    pv_by_key: dict[HorizonSlot, dict],
    *,
    now: datetime,
) -> tuple[dict[HorizonSlot, float], dict[HorizonSlot, str], dict[str, Any]]:
    """
    Zwraca skorygowane pv_kwh per slot oraz metadane korekty.

    - bieżąca h: A_so_far + (1−α)×F50×k_intra (gdy k_intra aktywne)
    - h+1: k_intra × F50
    - pozostałe: surowy F50 (pv_kw z Solcast)
    """
    if not slots:
        return {}, {}, {"enabled": PV_CORRECTION_ENABLED, "applied": False}

    current_slot = (now.date().isoformat(), now.hour)
    next_dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    next_slot: HorizonSlot = (next_dt.date().isoformat(), next_dt.hour)

    f50_row = pv_by_key.get(current_slot, {})
    f50_current = float(f50_row.get("pv_kw") or 0.0)
    state = build_pv_intra_state(now, f50_current_kwh=f50_current)
    k_intra = state.get("k_intra")

    corrected: dict[HorizonSlot, float] = {}
    sources: dict[HorizonSlot, str] = {}
    per_slot: list[dict[str, Any]] = []

    for slot in slots:
        row = pv_by_key.get(slot, {})
        f50 = float(row.get("pv_kw") or 0.0)
        source = "solcast_proxy"
        value = f50

        if k_intra is not None:
            if slot == current_slot:
                value, plan_meta = pv_plan_current_hour_kwh(
                    f50_kwh=f50,
                    a_so_far_kwh=float(state["a_so_far_kwh"]),
                    alpha=float(state["alpha"]),
                    k_intra=float(k_intra),
                    recent_kw=state.get("recent_kw"),
                )
                state["plan_method"] = plan_meta.get("method")
                state["pv_plan_kwh"] = value
                state["rate_plan_kwh"] = plan_meta.get("rate_plan_kwh")
                state["rate_blend_weight"] = plan_meta.get("rate_blend_weight")
                source = "pv_intra_current"
            elif slot == next_slot:
                value = pv_plan_next_hour_kwh(f50_kwh=f50, k_intra=float(k_intra))
                source = "pv_intra_next"

        corrected[slot] = value
        sources[slot] = source
        if source != "solcast_proxy":
            per_slot.append(
                {
                    "date": slot[0],
                    "hour": slot[1],
                    "pv_kwh": value,
                    "pv_kwh_p50": f50,
                    "source": source,
                }
            )

    meta = {
        **state,
        "current_slot": {"date": current_slot[0], "hour": current_slot[1]},
        "next_slot": {"date": next_slot[0], "hour": next_slot[1]},
        "slots_adjusted": per_slot,
    }
    return corrected, sources, meta
