"""Wejścia bieżącej godziny: energie pełnej h + ``hour_fraction`` tylko na moc."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from planner.load_correction import (
    LOAD_BAND_NARROW_ENABLED,
    apply_load_plan_to_meta,
    build_load_intra_meta,
    load_remainder_bands_kwh,
)
from planner.models import HourInputs
from planner.pv_correction import (
    PV_BAND_NARROW_ENABLED,
    hour_elapsed_fraction,
    pv_remainder_bands_kwh,
)
from planner.telemetry import net_kwh_so_far_for_hour


def hour_remaining_fraction(now: datetime, *, date: str, hour: int) -> float:
    """Ułamek bieżącej godziny pozostały do :00 (1.0 dla przyszłych slotów)."""
    if now.date().isoformat() != date or now.hour != hour:
        return 1.0
    return max(0.0, min(1.0, 1.0 - hour_elapsed_fraction(now)))


def balance_rhs_kwh(hin: HourInputs) -> float:
    """
    Prawa strona bilansu MILP: ``load_rem − pv_rem − N₀``.

    Przy pełnych energiach i so_far: optymalizator planuje **net końca godziny**
    (imp/exp = rozliczenie godzinowe), z wykonalnością mocy przez ``hour_fraction``.
    """
    load_so_far = float(hin.load_so_far_kwh or 0.0)
    pv_so_far = float(hin.pv_so_far_kwh or 0.0)
    n0 = float(hin.net_so_far_kwh or 0.0)
    load_rem = float(hin.load_kwh) - load_so_far
    pv_rem = float(hin.pv_kwh) - pv_so_far
    return load_rem - pv_rem - n0


def remaining_battery_delta_kwh(hin: HourInputs, net_end_kwh: float) -> float:
    """Δ baterii [kWh] od ``now`` do końca h (spójne z SOC₀→SOC_end)."""
    from planner.battery import battery_delta_from_net

    pv_so = float(hin.pv_so_far_kwh or 0.0)
    load_so = float(hin.load_so_far_kwh or 0.0)
    n0 = float(hin.net_so_far_kwh or 0.0)
    return battery_delta_from_net(
        pv_kwh=float(hin.pv_kwh) - pv_so,
        load_kwh=float(hin.load_kwh) - load_so,
        net_kwh=float(net_end_kwh) - n0,
    )


def _remaining_pv_kwh(
    hin: HourInputs,
    *,
    now: datetime,
    pv_correction_meta: dict[str, Any],
) -> tuple[float, float, float]:
    """Pasma PV na resztę bieżącej godziny (p50, p10, p90)."""
    full_p10 = hin.pv_kwh_p10 if hin.pv_kwh_p10 is not None else hin.pv_kwh
    full_p90 = hin.pv_kwh_p90 if hin.pv_kwh_p90 is not None else hin.pv_kwh
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


def _remaining_load_kwh(
    hin: HourInputs,
    *,
    now: datetime,
    load_meta: dict[str, Any],
) -> tuple[float, float, float]:
    """Pasma load na resztę bieżącej godziny (p50, p25, p75).

    Gdy jest telemetria: najpierw centralny ``load_plan`` (k_intra + rate blend),
    potem pasma wokół skorygowanego p50.
    """
    f50 = float(hin.load_kwh)
    full_p25 = float(hin.load_kwh_p25) if hin.load_kwh_p25 is not None else f50
    full_p75 = float(hin.load_kwh_p75) if hin.load_kwh_p75 is not None else f50
    frac = hour_remaining_fraction(now, date=hin.date, hour=hin.hour)
    a_so_far = load_meta.get("a_so_far_kwh")
    if a_so_far is not None:
        apply_load_plan_to_meta(load_meta, f50_kwh=f50)
        p50_full = f50
        plan = load_meta.get("load_plan_kwh")
        if plan is not None and float(plan) > 0.0 and f50 > 1e-9:
            p50_full = float(plan)
            scale = p50_full / f50
            full_p25 = full_p25 * scale
            full_p75 = full_p75 * scale
        elif plan is not None:
            p50_full = max(float(a_so_far), float(plan))

        narrow = load_meta.get("band_narrow_enabled", LOAD_BAND_NARROW_ENABLED)
        alpha = float(load_meta.get("alpha", hour_elapsed_fraction(now)))
        recent_kw = load_meta.get("recent_kw")
        if recent_kw is not None:
            recent_kw = float(recent_kw)
        return load_remainder_bands_kwh(
            p50_full=p50_full,
            p25_full=full_p25,
            p75_full=full_p75,
            a_so_far=float(a_so_far),
            alpha=alpha,
            recent_kw=recent_kw,
            narrow_enabled=bool(narrow),
        )

    return f50 * frac, full_p25 * frac, full_p75 * frac


def scale_hour_inputs_for_remainder(
    hin: HourInputs,
    *,
    now: datetime,
    pv_correction_meta: dict[str, Any],
    load_meta: dict[str, Any] | None = None,
) -> HourInputs:
    """
    Bieżący slot mid-hour: **energie pełnej godziny** (so_far + zwężona reszta),
    ``hour_fraction`` tylko do limitów mocy, ``net_so_far`` jako stan licznika.
    """
    frac = hour_remaining_fraction(now, date=hin.date, hour=hin.hour)
    if frac >= 1.0 - 1e-9:
        return hin

    load_m = load_meta if load_meta is not None else build_load_intra_meta(now)

    pv_rem, pv_p10_rem, pv_p90_rem = _remaining_pv_kwh(
        hin, now=now, pv_correction_meta=pv_correction_meta
    )
    load_rem, load_p25_rem, load_p75_rem = _remaining_load_kwh(
        hin, now=now, load_meta=load_m
    )

    pv_so_far_raw = pv_correction_meta.get("a_so_far_kwh")
    load_so_far_raw = load_m.get("a_so_far_kwh")
    # Bez telemetrii: rem = full×frac → so_far = full − rem (jednorodny rozkład).
    pv_so = (
        float(pv_so_far_raw)
        if pv_so_far_raw is not None
        else max(0.0, float(hin.pv_kwh) - pv_rem)
    )
    load_so = (
        float(load_so_far_raw)
        if load_so_far_raw is not None
        else max(0.0, float(hin.load_kwh) - load_rem)
    )

    # Pełna godzina = już zrobione + zwężona reszta (conditioned bands).
    pv_full = pv_so + pv_rem
    full_p10_in = hin.pv_kwh_p10 if hin.pv_kwh_p10 is not None else hin.pv_kwh
    full_p90_in = hin.pv_kwh_p90 if hin.pv_kwh_p90 is not None else hin.pv_kwh
    if pv_so_far_raw is None:
        pv_p10_full = full_p10_in
        pv_p90_full = full_p90_in
    else:
        pv_p10_full = pv_so + pv_p10_rem
        pv_p90_full = pv_so + pv_p90_rem

    load_full = load_so + load_rem
    full_p25_in = hin.load_kwh_p25 if hin.load_kwh_p25 is not None else hin.load_kwh
    full_p75_in = hin.load_kwh_p75 if hin.load_kwh_p75 is not None else hin.load_kwh
    if load_so_far_raw is None:
        load_p25_full = full_p25_in
        load_p75_full = full_p75_in
    else:
        load_p25_full = load_so + load_p25_rem
        load_p75_full = load_so + load_p75_rem
    load_p75_full = max(load_p75_full, load_full)
    load_p25_full = min(load_p25_full, load_full)

    net_so_far = net_kwh_so_far_for_hour(now.date(), now.hour)

    return hin.model_copy(
        update={
            "load_kwh": load_full,
            "load_kwh_p25": load_p25_full,
            "load_kwh_p75": load_p75_full,
            "pv_kwh": pv_full,
            "pv_kwh_p10": pv_p10_full,
            "pv_kwh_p90": pv_p90_full,
            "hour_fraction": frac,
            "net_so_far_kwh": net_so_far,
            "pv_so_far_kwh": pv_so,
            "load_so_far_kwh": load_so,
        }
    )
