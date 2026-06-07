"""Odczyt docelowego net kWh z rolling planu dla bieżącej godziny."""

from __future__ import annotations

from datetime import date

from planner.plan_store import hour_plan_from, load_latest_plan


def plan_target_net_kwh_for_hour(local_date: date, hour: int) -> float | None:
    """``target_net_kwh`` z plan_latest dla (data, godzina) lub None."""
    plan = load_latest_plan()
    if plan is None:
        return None
    hp = hour_plan_from(plan, local_date, hour)
    if hp is None:
        return None
    return float(hp.target_net_kwh)
