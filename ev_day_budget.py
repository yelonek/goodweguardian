"""Budżet ładowania dziennego: współczynnik × tania nadwyżka PV ponad zużycie domu.

Nadwyżka to ``PV p50 − load_base``, od godziny 8. Nie używa ``target_net`` planu,
żeby budżet nie zjadał sam siebie, gdy Planer już dostał to ładowanie.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ev_charging_plan import (
    EvChargingDeclaration,
    allocate_ev_schedule,
    build_horizon_slot_rows,
    current_delivered_ev_kwh,
    ev_schedule_map,
)
from guardian_config import EV_CHARGING_DEFAULT_POWER_KW, TELEMETRY_TZ
from pv_pyramid import BUDGET_FROM_HOUR, CHEAP_THRESHOLD_PLN, house_surplus_kwh

log = logging.getLogger("guardian")

_PAGE_CACHE: tuple[float, float, bool, dict[str, Any]] | None = None
_PAGE_CACHE_S = 60.0


def _local_now() -> datetime:
    return datetime.now(ZoneInfo(TELEMETRY_TZ)).replace(tzinfo=None)


@dataclass(frozen=True)
class DayChargingBudget:
    surplus_kwh: float
    budget_kwh: float
    coefficient: float
    window_start_hour: int | None
    window_end_hour: int | None
    threshold_pln: float = CHEAP_THRESHOLD_PLN

    def as_dict(self) -> dict[str, Any]:
        return {
            "surplus_kwh": self.surplus_kwh,
            "budget_kwh": self.budget_kwh,
            "coefficient": self.coefficient,
            "window_start_hour": self.window_start_hour,
            "window_end_hour": self.window_end_hour,
            "threshold_pln": self.threshold_pln,
        }


def compute_day_charging_budget(
    hours: list[dict[str, Any]],
    *,
    coefficient: float,
    from_hour: int = BUDGET_FROM_HOUR,
    threshold_pln: float = CHEAP_THRESHOLD_PLN,
) -> DayChargingBudget:
    """Godziny z ``pv_kwh``, ``load_base_kwh``, ``rce_pln_kwh``, ``hour``.

    ``export_kwh`` / ``target_net`` są ignorowane.
    """
    chosen: list[tuple[int, float]] = []
    for row in hours:
        hour = int(row["hour"])
        if hour < from_hour:
            continue
        rce = row.get("rce_pln_kwh")
        if rce is None or float(rce) >= threshold_pln:
            continue
        surplus = house_surplus_kwh(
            pv_kwh=float(row.get("pv_kwh") or 0.0),
            load_base_kwh=float(row.get("load_base_kwh") or 0.0),
        )
        if surplus <= 1e-9:
            continue
        chosen.append((hour, surplus))
    chosen.sort(key=lambda item: item[0])
    surplus_sum = sum(kwh for _, kwh in chosen)
    coeff = float(coefficient)
    budget = surplus_sum * coeff
    return DayChargingBudget(
        surplus_kwh=round(surplus_sum, 4),
        budget_kwh=round(budget, 4),
        coefficient=coeff,
        window_start_hour=chosen[0][0] if chosen else None,
        window_end_hour=chosen[-1][0] if chosen else None,
        threshold_pln=threshold_pln,
    )


def delivered_kwh_since_budget_hour(
    local_date,
    *,
    from_hour: int = BUDGET_FROM_HOUR,
) -> float:
    """kWh z licznika ładowarki od ``from_hour`` włącznie. Noc przed tą godziną nie wchodzi."""
    from tesla_wall_charger import hourly_ev_kwh_from_telemetry, twc_enabled

    if not twc_enabled():
        return 0.0
    by_hour = hourly_ev_kwh_from_telemetry(local_date)
    total = sum(float(kwh) for hour, kwh in by_hour.items() if int(hour) >= from_hour)
    return round(total, 4)


def charging_page_view(
    now: datetime | None = None,
    *,
    hours: list[dict[str, Any]] | None = None,
    delivered_since_8_kwh: float | None = None,
) -> dict[str, Any]:
    """Widok strony Ładowanie: budżet, okno, współczynnik, przełącznik, kWh od 8:00."""
    from guardian_settings import get_settings

    global _PAGE_CACHE
    settings = get_settings()
    now_local = (now or _local_now()).replace(tzinfo=None)
    coeff = float(settings.ev_charging_budget_coefficient)
    enabled = bool(settings.ev_charging_plan_enabled)
    if hours is None and delivered_since_8_kwh is None and _PAGE_CACHE is not None:
        age_s, cached_coeff, cached_enabled, cached = _PAGE_CACHE
        if (
            time.monotonic() - age_s < _PAGE_CACHE_S
            and cached_coeff == coeff
            and cached_enabled == enabled
        ):
            fresh = dict(cached)
            fresh["delivered_since_8_kwh"] = delivered_kwh_since_budget_hour(now_local.date())
            fresh["plan_enabled"] = enabled
            fresh["coefficient"] = coeff
            return fresh
    if hours is None:
        slots = [(now_local.date().isoformat(), h) for h in range(BUDGET_FROM_HOUR, 24)]
        hours = build_horizon_slot_rows(slots, use_rolling_plan=False)
    budget = compute_day_charging_budget(hours, coefficient=coeff)
    delivered = (
        delivered_kwh_since_budget_hour(now_local.date())
        if delivered_since_8_kwh is None
        else float(delivered_since_8_kwh)
    )
    payload = budget.as_dict()
    payload["plan_enabled"] = enabled
    payload["delivered_since_8_kwh"] = round(delivered, 4)
    if delivered_since_8_kwh is None:
        _PAGE_CACHE = (time.monotonic(), coeff, enabled, payload)
    return payload


def ev_schedule_for_planner(
    now: datetime,
    *,
    enabled: bool,
    coefficient: float,
    slot_rows: list[dict[str, Any]] | None = None,
) -> tuple[dict[tuple[str, int], float], dict[str, Any]]:
    """Harmonogram EV dla Planera. Wyłączony przełącznik → pusto, plik deklaracji się nie liczy.

    ``target_kwh`` to budżet plus kWh z nocy (przed 8:00), żeby noc nie ścinała dnia.
    Przyszłe sloty dostają to, co z budżetu jeszcze nie weszło od 8:00.
    """
    if not enabled:
        return {}, {}
    local_date = now.date().isoformat()
    rows = slot_rows
    if rows is None:
        slots = [(local_date, h) for h in range(BUDGET_FROM_HOUR, 24)]
        rows = build_horizon_slot_rows(slots, use_rolling_plan=False)
    else:
        rows = [r for r in rows if int(r["hour"]) >= BUDGET_FROM_HOUR]
    budget = compute_day_charging_budget(rows, coefficient=coefficient)
    if budget.budget_kwh <= 1e-9:
        return {}, {"budget": budget.as_dict(), "slots": []}
    delivered_all = current_delivered_ev_kwh(now.date(), now=now)
    since_8 = delivered_kwh_since_budget_hour(now.date())
    night = max(0.0, delivered_all - since_8)
    decl = EvChargingDeclaration(
        date=local_date,
        target_kwh=budget.budget_kwh + night,
        max_power_kw=EV_CHARGING_DEFAULT_POWER_KW,
    )
    plan = allocate_ev_schedule(decl, rows, now=now)
    schedule = ev_schedule_map(plan)
    dump = plan.model_dump()
    dump["budget"] = budget.as_dict()
    return schedule, dump
