"""Pełnogodzinna semantyka ``HourPlan`` eksportowanego do policy (§13 PLANNING_SYSTEM.md).

MILP w środku godziny operuje na slocie przeskalowanym ``hour_fraction``; pola
``target_net_kwh`` / ``battery_delta_kwh`` w policy muszą opisywać **całą godzinę**
(bilans licznika na :00, ekwiwalent mocy baterii), nie tylko resztę slotu.
"""

from __future__ import annotations

from datetime import datetime

from planner.models import HourInputs, HourPlan
from planner.telemetry import net_kwh_so_far_for_hour


def _full_hour_target_net_kwh(
    *,
    remainder_net: float,
    frac: float,
    net_so_far: float | None,
    is_current_hour: bool,
) -> float:
    """
    Cel bilansu na koniec pełnej godziny.

    Przy telemetrii bieżącej h: ``net_so_far + remainder_net`` (warunek początkowy
    + plan MILP na resztę). Bez telemetrii: ekstrapolacja ``remainder_net / frac``.
    """
    if frac >= 1.0 - 1e-9:
        return remainder_net
    if is_current_hour and net_so_far is not None:
        return net_so_far + remainder_net
    return remainder_net / frac


def normalize_hour_plans_for_policy(
    hours_in: list[HourInputs],
    plans: list[HourPlan],
    *,
    now: datetime,
) -> list[HourPlan]:
    """
    Konwertuje wynik MILP na reszcie bieżącej h → cele na koniec pełnej godziny.

    ``target_net_kwh`` = plan na pełną godzinę dla Guardiana / dashboardu:
    przy telemetrii bieżącej h ``net_so_far + remainder_net`` (także gdy
    ``remainder_net ≈ 0`` — setpoint Flappy trzyma już zrobiony bilans).

    Bez telemetrii: ``remainder_net / hour_fraction``.
    ``battery_delta_kwh``: ``remainder_bd / hour_fraction`` (ekwiwalent % mocy/h).
    Intencja trybu (import/eksport/soak) nadal z ``target_net_remainder_kwh``.
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
        full_net = _full_hour_target_net_kwh(
            remainder_net=remainder_net,
            frac=frac,
            net_so_far=net_so_far,
            is_current_hour=(hin.date == d_iso and hin.hour == h_now),
        )

        remainder_bd = float(hp.battery_delta_kwh)
        full_bd = remainder_bd / frac

        out.append(
            hp.model_copy(
                update={
                    "target_net_kwh": full_net,
                    "target_net_remainder_kwh": remainder_net,
                    "battery_delta_kwh": full_bd,
                }
            )
        )
    return out
