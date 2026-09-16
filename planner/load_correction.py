"""Korekta load mid-hour: k_intra + rate blend + zwężanie pasm (analog PV)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from planner.intra_hour import (
    clip_k,
    compute_k_prev_hour as _compute_k_prev_hour,
    correction_spill_minutes,
    energy_in_hour,
    hour_elapsed_fraction,
    minute_series_in_hour,
    mix_k_into_hour,
    rate_blend_weight,
    recent_average_kw,
)

# Kill-switch i tempo — const jak PV_BAND_* / PV_CORRECTION_*.
LOAD_CORRECTION_ENABLED = True
LOAD_CORRECTION_EPS_KWH = 0.1
LOAD_CORRECTION_K_MIN = 0.65
LOAD_CORRECTION_K_MAX = 1.35
LOAD_CORRECTION_RATE_ENABLED = True
LOAD_CORRECTION_RATE_BLEND_START = 0.2
LOAD_CORRECTION_RATE_BLEND_END = 0.7

LOAD_BAND_NARROW_ENABLED = True
LOAD_BAND_RATE_WINDOW_MIN = 15
LOAD_BAND_RATE_P25_FACTOR = 0.70
LOAD_BAND_RATE_P75_FACTOR = 1.15
LOAD_BAND_RATE_MIN_ALPHA = 0.15

LOAD_K_CARRY_ALPHA = 0.15
LOAD_K_PREV_MIN_SAMPLES = 15
LOAD_CORRECTION_HORIZON_MIN = 60


def _rate_blend_weight(
    alpha: float,
    *,
    blend_start: float = LOAD_CORRECTION_RATE_BLEND_START,
    blend_end: float = LOAD_CORRECTION_RATE_BLEND_END,
) -> float:
    return rate_blend_weight(alpha, blend_start=blend_start, blend_end=blend_end)


def load_recent_average_kw(
    now: datetime,
    *,
    window_min: int = LOAD_BAND_RATE_WINDOW_MIN,
) -> tuple[float, int] | None:
    """Średnia moc load [kW] z ostatnich ``window_min`` minut bieżącej godziny."""
    return recent_average_kw(
        now, window_min=window_min, power_field="consumption_w", log_label="load"
    )


def load_minute_series_in_hour(now: datetime) -> list[dict[str, float | int]]:
    """Minutowa kumulacja load [kWh] w bieżącej godzinie lokalnej."""
    return minute_series_in_hour(
        now, power_field="consumption_w", kw_key="load_kw", log_label="load"
    )


def load_energy_in_hour(
    *,
    local_date: str,
    hour: int,
    until_minute: int | None = None,
) -> tuple[float, int] | None:
    """Energia load [kWh] w godzinie lokalnej. ``until_minute`` włącznie; None = cała h."""
    return energy_in_hour(
        local_date=local_date,
        hour=hour,
        until_minute=until_minute,
        power_field="consumption_w",
        log_label="load",
    )


def load_energy_so_far_in_hour(now: datetime) -> tuple[float, int] | None:
    """Energia load [kWh] od początku bieżącej godziny lokalnej."""
    return load_energy_in_hour(
        local_date=now.date().isoformat(),
        hour=now.hour,
        until_minute=now.minute,
    )


def compute_load_k_intra_detail(
    *,
    f50_kwh: float,
    a_so_far_kwh: float,
    alpha: float,
    eps_kwh_per_h: float = LOAD_CORRECTION_EPS_KWH,
    k_min: float = LOAD_CORRECTION_K_MIN,
    k_max: float = LOAD_CORRECTION_K_MAX,
) -> tuple[float | None, str, dict[str, Any]]:
    """
    k_intra load: ``A / (α · F50)`` z clipem.

    Gdy F_elapsed <= ε×α — brak sensownego stosunku (początek godziny / śmieci).
    """
    meta: dict[str, Any] = {
        "k_raw": None,
        "k_intra": None,
        "clip_min": k_min,
        "clip_max": k_max,
        "f_elapsed_kwh": alpha * f50_kwh if alpha > 0 else 0.0,
    }
    if alpha <= 0.0:
        return None, "hour_start", meta
    f_elapsed = alpha * f50_kwh
    meta["f_elapsed_kwh"] = f_elapsed
    if f_elapsed <= eps_kwh_per_h * alpha:
        return None, "f_elapsed_below_eps", meta

    k_raw = a_so_far_kwh / f_elapsed
    k_intra = clip_k(k_raw, k_min=k_min, k_max=k_max)
    meta.update({"k_raw": k_raw, "k_intra": k_intra})
    return k_intra, "ok", meta


def compute_load_k_intra(
    *,
    f50_kwh: float,
    a_so_far_kwh: float,
    alpha: float,
    eps_kwh_per_h: float = LOAD_CORRECTION_EPS_KWH,
    k_min: float = LOAD_CORRECTION_K_MIN,
    k_max: float = LOAD_CORRECTION_K_MAX,
) -> tuple[float | None, str]:
    k_intra, reason, _ = compute_load_k_intra_detail(
        f50_kwh=f50_kwh,
        a_so_far_kwh=a_so_far_kwh,
        alpha=alpha,
        eps_kwh_per_h=eps_kwh_per_h,
        k_min=k_min,
        k_max=k_max,
    )
    return k_intra, reason


def mix_load_base_keep_ev(
    *,
    load_base: float,
    ev_kwh: float,
    k: float,
    spill_min: float,
    load_p25: float,
    load_p75: float,
) -> tuple[float, float, float, float]:
    """k miesza tylko ``load_base``; EV dodawane po mix. (base, total, p25, p75)."""
    base = mix_k_into_hour(f50=load_base, k=k, spill_min=spill_min)
    p25 = mix_k_into_hour(f50=load_base, k=k, spill_min=spill_min, q_raw=load_p25)
    p75 = mix_k_into_hour(f50=load_base, k=k, spill_min=spill_min, q_raw=load_p75)
    total = max(0.0, base + float(ev_kwh))
    if ev_kwh > 0:
        p75 = max(p75, total)
    return base, total, p25, p75


def compute_load_k_prev_hour(
    now: datetime,
    *,
    f50_prev_kwh: float,
) -> tuple[float | None, dict[str, Any]]:
    """k_load z pełnej poprzedniej godziny (sygnał domu)."""
    return _compute_k_prev_hour(
        now,
        f50_prev_kwh=f50_prev_kwh,
        energy_in_hour_fn=load_energy_in_hour,
        k_detail_fn=compute_load_k_intra_detail,
        min_samples=LOAD_K_PREV_MIN_SAMPLES,
    )


def load_plan_current_hour_kwh(
    *,
    f50_kwh: float,
    a_so_far_kwh: float,
    alpha: float,
    k_intra: float,
    recent_kw: float | None = None,
    rate_enabled: bool = LOAD_CORRECTION_RATE_ENABLED,
) -> tuple[float, dict[str, Any]]:
    """
    Prognoza na pełną bieżącą godzinę load [kWh/h].

    Bazowo: ``A_so_far + (1−α) × F50 × k_intra``.
    Opcjonalnie blend z estymatą rate: ``A_so_far + recent_kw × (1−α)``.
    """
    remaining = (1.0 - alpha) * f50_kwh * k_intra
    k_plan = max(0.0, a_so_far_kwh + remaining)
    meta: dict[str, Any] = {
        "method": "k_intra",
        "k_plan_kwh": k_plan,
        "rate_plan_kwh": None,
        "rate_blend_weight": 0.0,
        "recent_kw": recent_kw,
        "k_intra": k_intra,
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


def load_remainder_bands_kwh(
    *,
    p50_full: float,
    p25_full: float,
    p75_full: float,
    a_so_far: float,
    alpha: float,
    recent_kw: float | None = None,
    narrow_enabled: bool | None = None,
) -> tuple[float, float, float]:
    """
    Pasma load [kWh] na **resztę** bieżącej godziny (p50, p25, p75).

    Niepewność zwęża się z ``(1 − α)``; ``P*_total ≥ A_so_far``; opcjonalny
    floor reszty z ``recent_kw`` gdy dom nadal ciągnie (w tym **p50**).
    """
    if narrow_enabled is None:
        narrow_enabled = LOAD_BAND_NARROW_ENABLED

    p50_f = max(0.0, float(p50_full))
    p25_f = max(0.0, float(p25_full))
    p75_f = max(0.0, float(p75_full))
    a = max(0.0, float(a_so_far))
    al = max(0.0, min(1.0, float(alpha)))

    if not narrow_enabled:
        return (
            max(0.0, p50_f - a),
            max(0.0, p25_f - a),
            max(0.0, p75_f - a),
        )

    p25_tot = max(p25_f, a)
    p50_tot = max(p50_f, a)
    p75_tot = max(p75_f, a)
    width_tot = max(0.0, p75_tot - p25_tot)

    u = max(0.0, 1.0 - al)
    p50_rem = max(0.0, p50_tot - a)
    half = 0.5 * width_tot * u
    p25_rem = max(0.0, p50_rem - half)
    p75_rem = p50_rem + half

    if (
        recent_kw is not None
        and float(recent_kw) > 0.0
        and al >= LOAD_BAND_RATE_MIN_ALPHA
    ):
        frac = max(0.0, 1.0 - al)
        rate_p50 = float(recent_kw) * frac
        rate_p25 = rate_p50 * LOAD_BAND_RATE_P25_FACTOR
        rate_p75 = rate_p50 * LOAD_BAND_RATE_P75_FACTOR
        # Najpierw p50 (centralna baza), potem pasma — żeby floor p25 nie ginął.
        p50_rem = max(p50_rem, rate_p50)
        p25_rem = max(p25_rem, rate_p25)
        p75_rem = max(p75_rem, rate_p75)

    p25_rem = min(p25_rem, p50_rem)
    p75_rem = max(p75_rem, p50_rem)
    return p50_rem, p25_rem, p75_rem


def build_load_intra_meta(
    now: datetime,
    *,
    f50_current_kwh: float | None = None,
    f50_prev_kwh: float | None = None,
) -> dict[str, Any]:
    """Meta load so-far / tempo; opcjonalnie k z carry jak PV."""
    alpha = hour_elapsed_fraction(now)
    energy = load_energy_so_far_in_hour(now)
    a_so_far = float(energy[0]) if energy is not None else None
    samples = int(energy[1]) if energy is not None else 0
    recent = load_recent_average_kw(now)
    recent_kw = float(recent[0]) if recent is not None else None
    recent_samples = int(recent[1]) if recent is not None else 0
    spill_min = correction_spill_minutes(alpha, horizon_min=LOAD_CORRECTION_HORIZON_MIN)
    meta: dict[str, Any] = {
        "enabled": LOAD_CORRECTION_ENABLED,
        "band_narrow_enabled": LOAD_BAND_NARROW_ENABLED,
        "applied": False,
        "alpha": alpha,
        "a_so_far_kwh": a_so_far,
        "telemetry_samples": samples,
        "recent_kw": recent_kw,
        "recent_samples": recent_samples,
        "k_intra": None,
        "k_intra_current": None,
        "k_intra_source": None,
        "reason": "pending",
        "plan_method": None,
        "load_plan_kwh": None,
        "rate_plan_kwh": None,
        "rate_blend_weight": None,
        "horizon_min": LOAD_CORRECTION_HORIZON_MIN,
        "spill_min": spill_min,
        "mix_w": max(0.0, min(1.0, spill_min / 60.0)),
    }
    if not LOAD_CORRECTION_ENABLED:
        meta["reason"] = "disabled"
        return meta

    f50_prev = 0.0 if f50_prev_kwh is None else float(f50_prev_kwh)
    k_prev, prev_meta = compute_load_k_prev_hour(now, f50_prev_kwh=f50_prev)
    meta.update(prev_meta)

    k_intra: float | None = None
    reason = "no_telemetry"
    if a_so_far is not None and f50_current_kwh is not None:
        k_intra, reason, k_detail = compute_load_k_intra_detail(
            f50_kwh=float(f50_current_kwh),
            a_so_far_kwh=a_so_far,
            alpha=alpha,
        )
        meta.update(k_detail)
        meta["k_intra_current"] = k_intra

    if alpha < LOAD_K_CARRY_ALPHA and k_prev is not None:
        meta["k_intra"] = k_prev
        meta["reason"] = "prev_hour_load"
        meta["applied"] = True
        meta["k_intra_source"] = "prev_hour"
        if a_so_far is None:
            meta["a_so_far_kwh"] = 0.0
        return meta

    if a_so_far is not None and f50_current_kwh is None:
        meta["reason"] = "pending"
        return meta

    meta["k_intra"] = k_intra
    meta["reason"] = reason if a_so_far is not None else "pending"
    meta["applied"] = k_intra is not None
    meta["k_intra_source"] = "current_hour" if k_intra is not None else None
    return meta


def apply_load_plan_to_meta(
    load_meta: dict[str, Any],
    *,
    f50_kwh: float,
) -> dict[str, Any]:
    """
    Uzupełnia ``load_meta`` o k_intra / load_plan dla bieżącej godziny.

    Mutuje i zwraca ten sam dict (wygodnie dla snapshotu).
    """
    if not LOAD_CORRECTION_ENABLED:
        load_meta["reason"] = "disabled"
        load_meta["applied"] = False
        return load_meta

    a_so_far = load_meta.get("a_so_far_kwh")
    alpha = float(load_meta.get("alpha") or 0.0)
    k_intra = load_meta.get("k_intra")
    reason = str(load_meta.get("reason") or "")

    if k_intra is None or reason in {"pending", "no_telemetry", "hour_start", "f_elapsed_below_eps"}:
        if a_so_far is None:
            if k_intra is None:
                load_meta["reason"] = "no_telemetry"
                load_meta["applied"] = False
                return load_meta
            a_so_far = 0.0
            load_meta["a_so_far_kwh"] = 0.0
        else:
            k_intra, reason, k_detail = compute_load_k_intra_detail(
                f50_kwh=float(f50_kwh),
                a_so_far_kwh=float(a_so_far),
                alpha=alpha,
            )
            load_meta.update(
                {
                    "k_raw": k_detail.get("k_raw"),
                    "f_elapsed_kwh": k_detail.get("f_elapsed_kwh"),
                    "k_intra": k_intra,
                    "reason": reason,
                    "applied": False,
                }
            )
            if k_intra is None:
                return load_meta

    if a_so_far is None:
        a_so_far = 0.0
        load_meta["a_so_far_kwh"] = 0.0

    plan, plan_meta = load_plan_current_hour_kwh(
        f50_kwh=float(f50_kwh),
        a_so_far_kwh=float(a_so_far),
        alpha=alpha,
        k_intra=float(k_intra),
        recent_kw=load_meta.get("recent_kw"),
    )
    load_meta.update(
        {
            "applied": True,
            "load_plan_kwh": plan,
            "plan_method": plan_meta.get("method"),
            "rate_plan_kwh": plan_meta.get("rate_plan_kwh"),
            "rate_blend_weight": plan_meta.get("rate_blend_weight"),
            "k_plan_kwh": plan_meta.get("k_plan_kwh"),
        }
    )
    return load_meta
