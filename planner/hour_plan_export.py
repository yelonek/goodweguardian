"""Pełnogodzinna semantyka ``HourPlan`` eksportowanego do policy (§13 PLANNING_SYSTEM.md).

MILP w środku godziny operuje na slocie przeskalowanym ``hour_fraction``; pola
``target_net_kwh`` / ``battery_delta_kwh`` w policy muszą opisywać **całą godzinę**
(bilans licznika na :00, ekwiwalent mocy baterii), nie tylko resztę slotu.
"""

from __future__ import annotations

from datetime import date, datetime

from planner.models import HourInputs, HourPlan
from planner.telemetry import net_kwh_so_far_for_hour

# Import z początku h (np. EV) nie kotwiczy celu po replanowaniu — reszta slotu może wyrównać.
_OBSOLETE_IMPORT_ANCHOR_EPS = 1e-9


def _full_hour_target_net_kwh(
    *,
    remainder_net: float,
    frac: float,
    net_so_far: float | None,
    is_current_hour: bool,
) -> float:
    if frac >= 1.0 - 1e-9:
        return remainder_net
    extrapolated = remainder_net / frac
    if not is_current_hour or net_so_far is None:
        return extrapolated
    if net_so_far < 0.0 and remainder_net >= -_OBSOLETE_IMPORT_ANCHOR_EPS:
        return extrapolated
    return net_so_far + remainder_net


def normalize_hour_plans_for_policy(
    hours_in: list[HourInputs],
    plans: list[HourPlan],
    *,
    now: datetime,
) -> list[HourPlan]:
    """
    Konwertuje wynik MILP na reszcie bieżącej h → cele na koniec pełnej godziny.

    ``target_net_kwh`` = plan na pełną godzinę dla Guardiana / dashboardu.

    Gdy w środku h jest już import z **przestarzałego** planu (ujemny ``net_so_far``),
    a MILP na resztę nie dokłada importu (``remainder_net ≥ 0``), cel to ekstrapolacja
    ``remainder / hour_fraction`` — replan może wyrównać zamiast utrwalać −0,24 kWh.

    W pozostałych przypadkach z telemetrią: ``net_so_far + remainder_net``.
    Bez telemetrii: ``remainder_net / hour_fraction``.
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
                    "battery_delta_kwh": full_bd,
                }
            )
        )
    return out
