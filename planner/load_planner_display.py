"""Load widoczne w MILP — pełna godzina + reszta slotu (dashboard / audyt)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from planner.hour_remainder import scale_hour_inputs_for_remainder
from planner.load_correction import (
    LOAD_BAND_NARROW_ENABLED,
    LOAD_CORRECTION_ENABLED,
    apply_load_plan_to_meta,
    build_load_intra_meta,
)
from planner.models import HourInputs


def planner_load_milp_snapshot(
    *,
    now: datetime,
    date_iso: str,
    hour: int,
    load_p50_kwh: float,
    load_p25_kwh: float | None = None,
    load_p75_kwh: float | None = None,
    load_meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Pola load jak w ``HourInputs`` po ``scale_hour_inputs_for_remainder`` (wejście MILP).

    Zwraca ``None`` poza bieżącym slotem lokalnym.
    """
    if now.date().isoformat() != date_iso or now.hour != hour:
        return None

    p50 = max(0.0, float(load_p50_kwh))
    p25 = max(0.0, float(load_p25_kwh) if load_p25_kwh is not None else p50)
    p75 = max(0.0, float(load_p75_kwh) if load_p75_kwh is not None else p50)

    meta = dict(load_meta) if load_meta is not None else build_load_intra_meta(now)
    if LOAD_CORRECTION_ENABLED and meta.get("a_so_far_kwh") is not None:
        apply_load_plan_to_meta(meta, f50_kwh=p50)

    hin = HourInputs(
        date=date_iso,
        hour=hour,
        load_kwh=p50,
        pv_kwh=0.0,
        import_pln_per_kwh=0.0,
        export_pln_per_kwh=0.0,
        load_kwh_p25=p25,
        load_kwh_p75=p75,
    )
    scaled = scale_hour_inputs_for_remainder(
        hin,
        now=now,
        pv_correction_meta={},
        load_meta=meta,
    )
    frac = float(scaled.hour_fraction or 1.0)
    a_so = float(scaled.load_so_far_kwh or 0.0)
    rem_p50 = max(0.0, float(scaled.load_kwh) - a_so)
    rem_p25 = max(0.0, float(scaled.load_kwh_p25 or 0.0) - a_so)
    rem_p75 = max(0.0, float(scaled.load_kwh_p75 or 0.0) - a_so)

    return {
        "load_planner_active": True,
        "load_planner_hour_fraction": round(frac, 4),
        "load_planner_alpha": meta.get("alpha"),
        "load_planner_a_so_far_kwh": meta.get("a_so_far_kwh"),
        "load_planner_k_intra": meta.get("k_intra"),
        "load_planner_plan_method": meta.get("plan_method"),
        "load_planner_band_narrow_enabled": meta.get(
            "band_narrow_enabled", LOAD_BAND_NARROW_ENABLED
        ),
        "load_planner_full_p25_kwh": round(float(scaled.load_kwh_p25 or 0.0), 4),
        "load_planner_full_p50_kwh": round(float(scaled.load_kwh), 4),
        "load_planner_full_p75_kwh": round(float(scaled.load_kwh_p75 or 0.0), 4),
        "load_planner_remainder_p25_kwh": round(rem_p25, 4),
        "load_planner_remainder_p50_kwh": round(rem_p50, 4),
        "load_planner_remainder_p75_kwh": round(rem_p75, 4),
    }
