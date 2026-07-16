"""PV widoczne w MILP — pełna godzina + reszta slotu (dashboard / audyt)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from planner.hour_remainder import hour_remaining_fraction, scale_hour_inputs_for_remainder
from planner.models import HourInputs
from planner.pv_correction import (
    PV_BAND_NARROW_ENABLED,
    apply_pv_correction,
    build_pv_intra_state,
)


def scaled_pv_bands_from_solcast(
    pv_row: dict[str, Any],
    *,
    corrected_p50: float | None,
) -> tuple[float, float, float]:
    """p50/p10/p90 pełnej godziny — jak ``planner/inputs.py`` (k_scale po k_intra)."""
    p50_raw = float(pv_row.get("pv_kw") or 0.0)
    p50 = float(corrected_p50) if corrected_p50 is not None else p50_raw
    p10_raw = float(
        pv_row.get("pv_kw_p10") if pv_row.get("pv_kw_p10") is not None else p50_raw
    )
    p90_raw = float(
        pv_row.get("pv_kw_p90") if pv_row.get("pv_kw_p90") is not None else p50_raw
    )
    if p50_raw > 1e-9 and corrected_p50 is not None:
        k_scale = p50 / p50_raw
        return p50, max(0.0, p10_raw * k_scale), max(0.0, p90_raw * k_scale)
    return p50, max(0.0, p10_raw), max(0.0, p90_raw)


def build_pv_correction_meta_for_slot(
    *,
    now: datetime,
    date_iso: str,
    hour: int,
    pv_row: dict[str, Any],
) -> dict[str, Any]:
    """Metadane k_intra + ``band_narrow_enabled`` dla jednego slotu bieżącej godziny."""
    slot = (date_iso, hour)
    f50_raw = float(pv_row.get("pv_kw") or 0.0)
    state = build_pv_intra_state(now, f50_current_kwh=f50_raw)
    _, _, apply_bundle = apply_pv_correction([slot], {slot: pv_row}, now=now)
    return {**state, **{k: apply_bundle.get(k) for k in apply_bundle if k != "slots_adjusted"}}


def planner_pv_milp_snapshot(
    *,
    now: datetime,
    date_iso: str,
    hour: int,
    pv_row: dict[str, Any] | None,
    pv_correction_meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Pola PV jak w ``HourInputs`` po ``scale_hour_inputs_for_remainder`` (wejście MILP).

    Zwraca ``None`` poza bieżącym slotem lokalnym lub gdy brak wiersza PV.
    """
    if pv_row is None:
        return None
    if now.date().isoformat() != date_iso or now.hour != hour:
        return None

    slot = (date_iso, hour)
    meta = pv_correction_meta or build_pv_correction_meta_for_slot(
        now=now, date_iso=date_iso, hour=hour, pv_row=pv_row
    )
    corrected, _, _ = apply_pv_correction([slot], {slot: pv_row}, now=now)
    pv_p50 = float(corrected.get(slot, float(pv_row.get("pv_kw") or 0.0)))
    _, pv_p10, pv_p90 = scaled_pv_bands_from_solcast(pv_row, corrected_p50=pv_p50)

    hin = HourInputs(
        date=date_iso,
        hour=hour,
        load_kwh=0.0,
        pv_kwh=pv_p50,
        import_pln_per_kwh=0.0,
        export_pln_per_kwh=0.0,
        pv_kwh_p10=pv_p10,
        pv_kwh_p90=pv_p90,
    )
    scaled = scale_hour_inputs_for_remainder(hin, now=now, pv_correction_meta=meta)
    frac = float(scaled.hour_fraction or 1.0)
    a_so = float(scaled.pv_so_far_kwh or 0.0)
    rem_p50 = max(0.0, float(scaled.pv_kwh) - a_so)
    rem_p10 = max(0.0, float(scaled.pv_kwh_p10 or 0.0) - a_so)
    rem_p90 = max(0.0, float(scaled.pv_kwh_p90 or 0.0) - a_so)

    return {
        "pv_planner_active": True,
        "pv_planner_hour_fraction": round(frac, 4),
        "pv_planner_alpha": meta.get("alpha"),
        "pv_planner_a_so_far_kwh": meta.get("a_so_far_kwh"),
        "pv_planner_k_intra": meta.get("k_intra"),
        "pv_planner_band_narrow_enabled": meta.get(
            "band_narrow_enabled", PV_BAND_NARROW_ENABLED
        ),
        "pv_planner_full_p10_kwh": round(float(scaled.pv_kwh_p10 or 0.0), 4),
        "pv_planner_full_p50_kwh": round(float(scaled.pv_kwh), 4),
        "pv_planner_full_p90_kwh": round(float(scaled.pv_kwh_p90 or 0.0), 4),
        "pv_planner_remainder_p10_kwh": round(rem_p10, 4),
        "pv_planner_remainder_p50_kwh": round(rem_p50, 4),
        "pv_planner_remainder_p90_kwh": round(rem_p90, 4),
    }
