"""Korekta load mid-hour: k_intra + rate blend + zwężanie pasm (analog PV)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from guardian_config import TELEMETRY_DIR
from planner.pv_correction import hour_elapsed_fraction

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

log = logging.getLogger("planner")


def _clip_k(value: float, *, k_min: float, k_max: float) -> float:
    return max(k_min, min(k_max, value))


def _rate_blend_weight(
    alpha: float,
    *,
    blend_start: float = LOAD_CORRECTION_RATE_BLEND_START,
    blend_end: float = LOAD_CORRECTION_RATE_BLEND_END,
) -> float:
    """0 na początku godziny → 1 gdy alpha >= blend_end."""
    if blend_end <= blend_start:
        return 0.0
    if alpha <= blend_start:
        return 0.0
    if alpha >= blend_end:
        return 1.0
    return (alpha - blend_start) / (blend_end - blend_start)


def load_recent_average_kw(
    now: datetime,
    *,
    window_min: int = LOAD_BAND_RATE_WINDOW_MIN,
) -> tuple[float, int] | None:
    """Średnia moc load [kW] z ostatnich ``window_min`` minut bieżącej godziny."""
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
                    power_kw.append(float(row.get("consumption_w", 0.0)) / 1000.0)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
    except OSError as e:
        log.debug("load recent average read failed %s: %s", path, e)
        return None

    if not power_kw:
        return None
    return sum(power_kw) / len(power_kw), len(power_kw)


def load_minute_series_in_hour(now: datetime) -> list[dict[str, float | int]]:
    """
    Minutowa kumulacja load [kWh] w bieżącej godzinie lokalnej.

    Jak PV: ostatnia znana moc w minucie × 1/60, skumulowana od :00.
    """
    path = TELEMETRY_DIR / f"telemetry_{now.date().isoformat()}.jsonl"
    target_hour = now.hour
    by_minute: dict[int, float] = {}

    if not path.exists():
        return []

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
                    by_minute[minute] = float(row.get("consumption_w", 0.0)) / 1000.0
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
    except OSError as e:
        log.debug("load minute series read failed %s: %s", path, e)
        return []

    if not by_minute:
        return []

    series: list[dict[str, float | int]] = []
    cum_kwh = 0.0
    for minute in sorted(by_minute):
        load_kw = by_minute[minute]
        cum_kwh += load_kw / 60.0
        series.append({"minute": minute, "load_kw": load_kw, "cum_kwh": cum_kwh})
    return series


def load_energy_so_far_in_hour(now: datetime) -> tuple[float, int] | None:
    """
    Energia load [kWh] od początku bieżącej godziny lokalnej.

    Zwraca ``(energia, liczba_próbek)`` lub ``None`` gdy brak telemetrii.
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
                    energy_kwh += float(row.get("consumption_w", 0.0)) / 1000.0 / 60.0
                    count += 1
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
    except OSError as e:
        log.debug("load energy read failed %s: %s", path, e)
        return None

    if count == 0:
        return None
    return energy_kwh, count


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
    k_intra = _clip_k(k_raw, k_min=k_min, k_max=k_max)
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


def build_load_intra_meta(now: datetime) -> dict[str, Any]:
    """Meta load so-far / tempo dla mid-hour (snapshot / hour_remainder)."""
    alpha = hour_elapsed_fraction(now)
    energy = load_energy_so_far_in_hour(now)
    a_so_far = float(energy[0]) if energy is not None else None
    samples = int(energy[1]) if energy is not None else 0
    recent = load_recent_average_kw(now)
    recent_kw = float(recent[0]) if recent is not None else None
    recent_samples = int(recent[1]) if recent is not None else 0
    return {
        "enabled": LOAD_CORRECTION_ENABLED,
        "band_narrow_enabled": LOAD_BAND_NARROW_ENABLED,
        "applied": False,
        "alpha": alpha,
        "a_so_far_kwh": a_so_far,
        "telemetry_samples": samples,
        "recent_kw": recent_kw,
        "recent_samples": recent_samples,
        "k_intra": None,
        "reason": "pending",
        "plan_method": None,
        "load_plan_kwh": None,
        "rate_plan_kwh": None,
        "rate_blend_weight": None,
    }


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
    if a_so_far is None:
        load_meta["reason"] = "no_telemetry"
        load_meta["applied"] = False
        return load_meta

    alpha = float(load_meta.get("alpha") or 0.0)
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
