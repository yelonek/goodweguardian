"""Planowane ładowanie EV — rekomendacja tanich slotów i alokacja harmonogramu."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from energy_pricing import pricing_day_breakdown
from guardian_config import TELEMETRY_TZ, TESLA_WC_MAX_KW
from pv_forecast import fetch_hourly_pv_forecast
from pv_pyramid import CHEAP_THRESHOLD_PLN
from tariff_g12 import G12TariffConfig, g12_tariff_from_env

log = logging.getLogger("guardian")

CHEAP_PV_MIN_KWH = 0.5


class EvChargingSlot(BaseModel):
    date: str
    hour: int = Field(ge=0, le=23)
    kwh: float = Field(ge=0)


class EvChargingDeclaration(BaseModel):
    date: str
    target_kwh: float = Field(ge=0)
    preferred_start_hour: int | None = Field(default=None, ge=0, le=23)
    max_power_kw: float = Field(default=TESLA_WC_MAX_KW, gt=0)
    manual_slots: dict[int, float] | None = None
    updated_at: str | None = None

    @field_validator("manual_slots", mode="before")
    @classmethod
    def _normalize_manual_slots(cls, v: Any) -> dict[int, float] | None:
        if v is None:
            return None
        if not isinstance(v, dict):
            raise TypeError("manual_slots must be a dict")
        out: dict[int, float] = {}
        for k, val in v.items():
            out[int(k)] = max(0.0, float(val))
        return out


class CheapBudget(BaseModel):
    cheap_pv_kwh: float
    cheap_import_kwh: float
    recommendable_kwh: float


class EvChargingPlan(BaseModel):
    date: str
    declaration: EvChargingDeclaration | None = None
    slots: list[EvChargingSlot] = Field(default_factory=list)
    cheap_budget: CheapBudget | None = None
    warnings: list[str] = Field(default_factory=list)
    recommended_slots: list[EvChargingSlot] = Field(default_factory=list)


def _local_now() -> datetime:
    return datetime.now(ZoneInfo(TELEMETRY_TZ))


def _slot_dt(d_iso: str, hour: int) -> datetime:
    return datetime.fromisoformat(f"{d_iso}T{hour:02d}:00:00")


def _slot_is_future_or_current(d_iso: str, hour: int, now: datetime) -> bool:
    slot_start = _slot_dt(d_iso, hour)
    return slot_start >= now.replace(minute=0, second=0, microsecond=0, tzinfo=None)


def is_cheap_slot(
    *,
    hour: int,
    rce_pln: float,
    pv_kwh: float,
    tariff: G12TariffConfig,
) -> bool:
    is_night = hour in tariff.night_hours
    cheap_pv = rce_pln < CHEAP_THRESHOLD_PLN and pv_kwh >= CHEAP_PV_MIN_KWH
    return is_night or cheap_pv


def slot_score(
    *,
    import_pln: float,
    rce_pln: float,
    pv_kwh: float,
) -> float:
    """Niższy = lepszy slot do ładowania."""
    opportunity = rce_pln * pv_kwh if pv_kwh > CHEAP_PV_MIN_KWH else 0.0
    return import_pln + opportunity


def build_horizon_slot_rows(
    slots: list[tuple[str, int]],
    *,
    pv_by_key: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    tariff = g12_tariff_from_env()
    pricing_cache: dict[str, dict[str, Any]] = {}
    pv_map = pv_by_key or {}

    if not pv_map and slots:
        try:
            pack = fetch_hourly_pv_forecast(hours=max(len(slots) + 2, 48))
            for r in pack.get("hours", []):
                pv_map[(str(r["date"]), int(r["hour"]))] = r
        except Exception as e:
            log.warning("EV plan: PV forecast unavailable: %s", e)

    rows: list[dict[str, Any]] = []
    for d_iso, hour in slots:
        if d_iso not in pricing_cache:
            pricing_cache[d_iso] = pricing_day_breakdown(date.fromisoformat(d_iso))
        pb = pricing_cache[d_iso]
        ph = pb["hours"][hour]
        import_pln = float(ph["import_pln_per_kwh"])
        rce_pln = float(ph["rce_pln_kwh"])
        pv_row = pv_map.get((d_iso, hour))
        pv_kwh = float(pv_row.get("pv_kw") or 0.0) if pv_row else 0.0
        is_night = hour in tariff.night_hours
        cheap = is_cheap_slot(hour=hour, rce_pln=rce_pln, pv_kwh=pv_kwh, tariff=tariff)
        rows.append(
            {
                "date": d_iso,
                "hour": hour,
                "import_pln_per_kwh": import_pln,
                "rce_pln_kwh": rce_pln,
                "pv_kwh": pv_kwh,
                "is_g12_night": is_night,
                "is_cheap": cheap,
                "score": slot_score(import_pln=import_pln, rce_pln=rce_pln, pv_kwh=pv_kwh),
            }
        )
    return rows


def compute_cheap_budget(
    slot_rows: list[dict[str, Any]],
    *,
    now: datetime,
    max_power_kw: float,
) -> CheapBudget:
    remaining = [
        r
        for r in slot_rows
        if _slot_is_future_or_current(str(r["date"]), int(r["hour"]), now)
    ]
    cheap_pv = sum(
        float(r["pv_kwh"])
        for r in remaining
        if float(r["rce_pln_kwh"]) < CHEAP_THRESHOLD_PLN
    )
    cheap_import = sum(
        max_power_kw for r in remaining if bool(r.get("is_g12_night"))
    )
    return CheapBudget(
        cheap_pv_kwh=round(cheap_pv, 4),
        cheap_import_kwh=round(cheap_import, 4),
        recommendable_kwh=round(cheap_pv + cheap_import, 4),
    )


def _greedy_allocate(
    slot_rows: list[dict[str, Any]],
    *,
    target_kwh: float,
    max_power_kw: float,
    local_date: str,
    now: datetime,
) -> list[EvChargingSlot]:
    if target_kwh <= 0:
        return []
    candidates = [
        r
        for r in slot_rows
        if r["date"] == local_date
        and _slot_is_future_or_current(str(r["date"]), int(r["hour"]), now)
    ]
    ranked = sorted(candidates, key=lambda r: (float(r["score"]), int(r["hour"])))
    remaining = target_kwh
    out: list[EvChargingSlot] = []
    for row in ranked:
        if remaining <= 1e-9:
            break
        cap = max_power_kw
        kwh = min(cap, remaining)
        out.append(
            EvChargingSlot(date=local_date, hour=int(row["hour"]), kwh=round(kwh, 4))
        )
        remaining -= kwh
    return out


def _preferred_start_allocate(
    slot_rows: list[dict[str, Any]],
    *,
    declaration: EvChargingDeclaration,
    now: datetime,
) -> tuple[list[EvChargingSlot], list[str]]:
    local_date = declaration.date
    start_h = declaration.preferred_start_hour
    if start_h is None:
        return [], []

    warnings: list[str] = []
    cheaper_before: list[dict[str, Any]] = []
    for row in slot_rows:
        if row["date"] != local_date:
            continue
        h = int(row["hour"])
        if h >= start_h:
            continue
        if not _slot_is_future_or_current(local_date, h, now):
            continue
        if bool(row.get("is_cheap")) and float(row["score"]) < 900.0:
            cheaper_before.append(row)

    if cheaper_before:
        pv_kwh = sum(float(r["pv_kwh"]) for r in cheaper_before if float(r["rce_pln_kwh"]) < CHEAP_THRESHOLD_PLN)
        if pv_kwh > 0.5:
            hrs = ", ".join(f"{int(r['hour']):02d}" for r in cheaper_before[:4])
            warnings.append(
                f"Godz. {hrs}: ~{pv_kwh:.1f} kWh taniego PV (<60 gr) — rozważ wcześniejsze ładowanie."
            )

    remaining = declaration.target_kwh
    out: list[EvChargingSlot] = []
    for h in range(start_h, 24):
        if remaining <= 1e-9:
            break
        if not _slot_is_future_or_current(local_date, h, now):
            continue
        kwh = min(declaration.max_power_kw, remaining)
        out.append(EvChargingSlot(date=local_date, hour=h, kwh=round(kwh, 4)))
        remaining -= kwh
    return out, warnings


def _manual_allocate(declaration: EvChargingDeclaration) -> list[EvChargingSlot]:
    if not declaration.manual_slots:
        return []
    total = sum(declaration.manual_slots.values())
    if total > declaration.target_kwh + 1e-6:
        raise ValueError(
            f"manual_slots suma {total:.2f} kWh > target_kwh {declaration.target_kwh:.2f}"
        )
    return [
        EvChargingSlot(date=declaration.date, hour=h, kwh=round(kwh, 4))
        for h, kwh in sorted(declaration.manual_slots.items())
        if kwh > 0
    ]


def allocate_ev_schedule(
    declaration: EvChargingDeclaration,
    slot_rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> EvChargingPlan:
    now_local = (now or _local_now()).replace(tzinfo=None)
    budget = compute_cheap_budget(
        slot_rows, now=now_local, max_power_kw=declaration.max_power_kw
    )
    warnings: list[str] = []
    recommended = _greedy_allocate(
        slot_rows,
        target_kwh=declaration.target_kwh,
        max_power_kw=declaration.max_power_kw,
        local_date=declaration.date,
        now=now_local,
    )

    if declaration.manual_slots:
        slots = _manual_allocate(declaration)
    elif declaration.preferred_start_hour is not None:
        slots, warnings = _preferred_start_allocate(
            slot_rows, declaration=declaration, now=now_local
        )
    else:
        slots = recommended

    if declaration.target_kwh > budget.recommendable_kwh + 0.1:
        warnings.append(
            f"Cel {declaration.target_kwh:.1f} kWh przekracza szacowany budżet tanio "
            f"({budget.recommendable_kwh:.1f} kWh)."
        )

    return EvChargingPlan(
        date=declaration.date,
        declaration=declaration,
        slots=slots,
        cheap_budget=budget,
        warnings=warnings,
        recommended_slots=recommended,
    )


def ev_schedule_map(plan: EvChargingPlan) -> dict[tuple[str, int], float]:
    return {(s.date, s.hour): s.kwh for s in plan.slots}


def slots_for_local_date(local_date: date, *, now: datetime | None = None) -> list[tuple[str, int]]:
    now_local = (now or _local_now()).replace(tzinfo=None)
    if local_date == now_local.date():
        start = now_local.replace(minute=0, second=0, microsecond=0)
    else:
        start = datetime(local_date.year, local_date.month, local_date.day)
    end = datetime(local_date.year, local_date.month, local_date.day, 23)
    slots: list[tuple[str, int]] = []
    cur = start
    while cur <= end:
        slots.append((cur.date().isoformat(), cur.hour))
        cur += timedelta(hours=1)
    return slots


def build_ev_recommendation(
    local_date: date | None = None,
    *,
    target_kwh: float | None = None,
    now: datetime | None = None,
    max_power_kw: float | None = None,
) -> EvChargingPlan:
    now_local = (now or _local_now()).replace(tzinfo=None)
    d = local_date or now_local.date()
    d_iso = d.isoformat()
    power = max_power_kw if max_power_kw is not None else TESLA_WC_MAX_KW
    slot_list = slots_for_local_date(d, now=now_local)
    rows = build_horizon_slot_rows(slot_list)
    budget = compute_cheap_budget(rows, now=now_local, max_power_kw=power)
    tgt = target_kwh if target_kwh is not None else budget.recommendable_kwh
    decl = EvChargingDeclaration(date=d_iso, target_kwh=max(0.0, tgt), max_power_kw=power)
    recommended = _greedy_allocate(
        rows,
        target_kwh=decl.target_kwh,
        max_power_kw=power,
        local_date=d_iso,
        now=now_local,
    )
    return EvChargingPlan(
        date=d_iso,
        declaration=None,
        slots=[],
        cheap_budget=budget,
        warnings=[],
        recommended_slots=recommended,
    )


def build_ev_charging_plan(
    declaration: EvChargingDeclaration | None = None,
    *,
    local_date: date | None = None,
    now: datetime | None = None,
) -> EvChargingPlan:
    now_local = (now or _local_now()).replace(tzinfo=None)
    d = local_date or (date.fromisoformat(declaration.date) if declaration else now_local.date())
    if declaration is None:
        return build_ev_recommendation(d, now=now_local)
    slot_list = slots_for_local_date(d, now=now_local)
    rows = build_horizon_slot_rows(slot_list)
    return allocate_ev_schedule(declaration, rows, now=now_local)
