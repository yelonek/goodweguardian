"""Skalowanie wejść planera na resztę bieżącej godziny (mid-hour rolling plan)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from planner.models import HourInputs
from planner.pv_correction import (
    PV_BAND_NARROW_ENABLED,
    hour_elapsed_fraction,
    pv_remainder_bands_kwh,
)


def hour_remaining_fraction(now: datetime, *, date: str, hour: int) -> float:
    """Ułamek bieżącej godziny pozostały do :00 (1.0 dla przyszłych slotów)."""
    if now.date().isoformat() != date or now.hour != hour:
        return 1.0
    return max(0.0, min(1.0, 1.0 - hour_elapsed_fraction(now)))


def _remaining_pv_kwh(
    hin: HourInputs,
    *,
    now: datetime,
    pv_correction_meta: dict[str, Any],
) -> tuple[float, float, float]:
    """Pozostała energia PV w slocie bieżącej godziny + pasma niepewności."""
    full_p10 = hin.pv_kwh_p10 if hin.pv_kwh_p10 is not None else hin.pv_kwh
    full_p90 = hin.pv_kwh_p90 if hin.pv_kwh_p90 is not None else hin.pv_kwh
    if now.date().isoformat() != hin.date or now.hour != hin.hour:
        return hin.pv_kwh, full_p10, full_p90

    frac = hour_remaining_fraction(now, date=hin.date, hour=hin.hour)
    a_so_far = pv_correction_meta.get("a_so_far_kwh")
    if a_so_far is not None:
        narrow = pv_correction_meta.get("band_narrow_enabled", PV_BAND_NARROW_ENABLED)
        alpha = hour_elapsed_fraction(now)
        recent_kw = pv_correction_meta.get("recent_kw")
        if recent_kw is not None:
            recent_kw = float(recent_kw)
        return pv_remainder_bands_kwh(
            p50_full=hin.pv_kwh,
            p10_full=full_p10,
            p90_full=full_p90,
            a_so_far=float(a_so_far),
            alpha=alpha,
            recent_kw=recent_kw,
            narrow_enabled=bool(narrow),
        )

    return hin.pv_kwh * frac, full_p10 * frac, full_p90 * frac


def scale_hour_inputs_for_remainder(
    hin: HourInputs,
    *,
    now: datetime,
    pv_correction_meta: dict[str, Any],
) -> HourInputs:
    """
    Dla bieżącego slotu w środku godziny: load/PV na resztę h + ``hour_fraction``
    dla limitów mocy w MILP.
    """
    frac = hour_remaining_fraction(now, date=hin.date, hour=hin.hour)
    if frac >= 1.0 - 1e-9:
        return hin

    load = hin.load_kwh * frac
    load_p75 = (hin.load_kwh_p75 if hin.load_kwh_p75 is not None else hin.load_kwh) * frac
    pv, pv_p10, pv_p90 = _remaining_pv_kwh(hin, now=now, pv_correction_meta=pv_correction_meta)

    return hin.model_copy(
        update={
            "load_kwh": load,
            "load_kwh_p75": load_p75,
            "pv_kwh": pv,
            "pv_kwh_p10": pv_p10,
            "pv_kwh_p90": pv_p90,
            "hour_fraction": frac,
        }
    )
