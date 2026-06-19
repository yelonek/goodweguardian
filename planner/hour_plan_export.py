"""Pełnogodzinna semantyka ``HourPlan`` eksportowanego do policy (§13 PLANNING_SYSTEM.md).

MILP w środku godziny operuje na slocie przeskalowanym ``hour_fraction``; pola
``target_net_kwh`` / ``battery_delta_kwh`` w policy muszą opisywać **całą godzinę**
(bilans licznika na :00, ekwiwalent mocy baterii), nie tylko resztę slotu.
"""

from __future__ import annotations

from datetime import date, datetime

from planner.models import HourInputs, HourPlan
from planner.telemetry import net_kwh_so_far_for_hour


def normalize_hour_plans_for_policy(
    hours_in: list[HourInputs],
    plans: list[HourPlan],
    *,
    now: datetime,
) -> list[HourPlan]:
    """
    Konwertuje wynik MILP na reszcie bieżącej h → cele na koniec pełnej godziny.

    ``target_net_kwh`` = faktyczny bilans od :00 (telemetria) + plan na resztę h;
    bez telemetrii: ``remainder_net / hour_fraction`` (ekstrapolacja liniowa).
    ``battery_delta_kwh``: ``remainder_bd / hour_fraction`` (ekwiwalent % mocy/h).
    """
    if len(hours_in) != len(plans):
        return plans

    d_iso = now.date().isoformat()
    h_now = now.hour
    net_so_far = net_kwh_so_far_for_hour(now.date(), h_now)

    out: list[HourPlan] = []
    for hin, hp in zip(hours_in, plans, strict=True):
        frac = float(hin.hour_fraction) if hin.hour_fraction > 0 else 1.0
        if frac >= 1.0 - 1e-9:
            out.append(hp)
            continue

        remainder_net = float(hp.target_net_kwh)
        if net_so_far is not None and hin.date == d_iso and hin.hour == h_now:
            full_net = net_so_far + remainder_net
        else:
            full_net = remainder_net / frac

        remainder_bd = float(hp.battery_delta_kwh)
        full_bd = remainder_bd / frac

        out.append(
            hp.model_copy(
                update={
                    "target_net_kwh": full_net,
                    "battery_delta_kwh": full_bd,
                }
            )
        )
    return out
