"""Pełnogodzinna semantyka ``HourPlan`` eksportowanego do policy (§13 PLANNING_SYSTEM.md).

MILP planuje **net końca godziny** (energie full-hour + ``N₀`` w bilansie).
``hour_fraction`` ogranicza tylko moc; ``battery_delta_kwh`` = Δ od ``now`` (SOC).
"""

from __future__ import annotations

from datetime import datetime

from planner.models import HourInputs, HourPlan


def normalize_hour_plans_for_policy(
    hours_in: list[HourInputs],
    plans: list[HourPlan],
    *,
    now: datetime,
) -> list[HourPlan]:
    """
    Uzupełnia ``target_net_remainder_kwh`` (= net* − N₀) do audytu.

    ``target_net_kwh`` / ``battery_delta_kwh`` z MILP są już w semantyce docelowej
    (net końca h; Δ baterii od teraz) — bez ekstrapolacji ``/ hour_fraction``.
    """
    del now  # API stabilne; N₀ jest na HourInputs
    if len(hours_in) != len(plans):
        return plans

    out: list[HourPlan] = []
    for hin, hp in zip(hours_in, plans, strict=True):
        n0 = float(hin.net_so_far_kwh or 0.0)
        net_end = float(hp.target_net_kwh)
        rem_net = net_end - n0 if hin.net_so_far_kwh is not None else None
        if rem_net is None and float(hin.hour_fraction) < 1.0 - 1e-9:
            rem_net = net_end
        out.append(
            hp.model_copy(
                update={
                    "target_net_remainder_kwh": rem_net,
                }
            )
        )
    return out
