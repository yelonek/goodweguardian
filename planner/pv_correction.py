"""Korekta prognozy PV: k_intra na bieżącą godzinę i h+1."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from guardian_config import (
    PV_CORRECTION_ENABLED,
    PV_CORRECTION_EPS_KWH,
    PV_CORRECTION_K_MAX,
    PV_CORRECTION_K_MIN,
    TELEMETRY_DIR,
)

log = logging.getLogger("planner")

HorizonSlot = tuple[str, int]


def hour_elapsed_fraction(now: datetime) -> float:
    """α = ułamek bieżącej godziny lokalnej (0 na :00:00)."""
    return (now.minute + now.second / 60.0) / 60.0


def clip_k(value: float, *, k_min: float, k_max: float) -> float:
    return max(k_min, min(k_max, value))


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


def compute_k_intra(
    *,
    f50_kwh: float,
    a_so_far_kwh: float,
    alpha: float,
    eps_kwh_per_h: float = PV_CORRECTION_EPS_KWH,
    k_min: float = PV_CORRECTION_K_MIN,
    k_max: float = PV_CORRECTION_K_MAX,
) -> tuple[float | None, str]:
    """
    k_intra = clip(A_so_far / F_elapsed, k_min, k_max).

    Gdy F_elapsed <= ε×α — brak sensownego stosunku (noc, początek godziny).
    """
    if alpha <= 0.0:
        return None, "hour_start"
    f_elapsed = alpha * f50_kwh
    if f_elapsed <= eps_kwh_per_h * alpha:
        return None, "f_elapsed_below_eps"
    return clip_k(a_so_far_kwh / f_elapsed, k_min=k_min, k_max=k_max), "ok"


def pv_plan_current_hour_kwh(
    *,
    f50_kwh: float,
    a_so_far_kwh: float,
    alpha: float,
    k_intra: float,
) -> float:
    """Prognoza na resztę bieżącej godziny + energia już wyprodukowana."""
    remaining = (1.0 - alpha) * f50_kwh * k_intra
    return max(0.0, a_so_far_kwh + remaining)


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

    meta: dict[str, Any] = {
        "enabled": PV_CORRECTION_ENABLED,
        "applied": False,
        "alpha": alpha,
        "f50_current_kwh": f50_current_kwh,
        "a_so_far_kwh": a_so_far,
        "telemetry_samples": samples,
        "f_elapsed_kwh": alpha * f50_current_kwh,
        "k_intra": None,
        "reason": "disabled",
    }

    if not PV_CORRECTION_ENABLED:
        return meta

    if a_so_far is None:
        meta["reason"] = "no_telemetry"
        return meta

    k_intra, reason = compute_k_intra(
        f50_kwh=f50_current_kwh,
        a_so_far_kwh=a_so_far,
        alpha=alpha,
    )
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
                value = pv_plan_current_hour_kwh(
                    f50_kwh=f50,
                    a_so_far_kwh=float(state["a_so_far_kwh"]),
                    alpha=float(state["alpha"]),
                    k_intra=float(k_intra),
                )
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
