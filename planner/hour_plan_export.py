"""Pełnogodzinna semantyka ``HourPlan`` eksportowanego do policy (§13 PLANNING_SYSTEM.md).

MILP w środku godziny operuje na slocie przeskalowanym ``hour_fraction``; pola
``target_net_kwh`` / ``battery_delta_kwh`` w policy muszą opisywać **całą godzinę**
(bilans licznika na :00, ekwiwalent mocy baterii), nie tylko resztę slotu.
"""

from __future__ import annotations

from datetime import datetime

from planner.models import HourInputs, HourPlan
from planner.telemetry import net_kwh_so_far_for_hour

# Gdy MILP nie planuje wymiany z siecią na resztę h — cel z planu, nie kotwica telemetrii.
_REMAINDER_NET_EPS = 1e-9


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
    # Reszta slotu bez importu/eksportu → nie utrwalaj net_so_far (+0,05 / −0,24 z początku h).
    if abs(remainder_net) <= _REMAINDER_NET_EPS:
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

    Gdy MILP na **resztę** bieżącej h nie planuje wymiany z siecią
    (``remainder_net ≈ 0``), cel to ``remainder_net / hour_fraction`` — zwykle 0.
    Telemetria ``net_so_far`` **nie kotwiczy** setpointu (ani import, ani eksport
    z pierwszych minut po replanowaniu).

    Gdy MILP planuje resztę h z siecią (``|remainder_net| > ε``): ``net_so_far + remainder_net``.
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
                    "target_net_remainder_kwh": remainder_net,
                    "battery_delta_kwh": full_bd,
                }
            )
        )
    return out
