"""Pełnogodzinna semantyka ``HourPlan`` eksportowanego do policy (§13 PLANNING_SYSTEM.md).

MILP planuje **net końca godziny** (energie full-hour + ``N₀`` w bilansie).
``hour_fraction`` ogranicza tylko moc; ``battery_delta_kwh`` = Δ od ``now`` (SOC).
"""

from __future__ import annotations

from datetime import datetime

from economics import settle_seller_hour
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
        net_end = float(hp.target_net_kwh)
        current = hin.net_so_far_kwh
        if current is None and float(hin.hour_fraction) < 1.0 - 1e-9:
            current = 0.0
        rem_net = None
        if current is not None:
            rem_net = settle_seller_hour(
                net_end,
                current_balance_kwh=float(current),
                rce_pln_per_kwh=hin.export_pln_per_kwh,
                import_pln_per_kwh=hin.import_pln_per_kwh,
            ).remainder_kwh
        out.append(
            hp.model_copy(
                update={
                    "target_net_remainder_kwh": rem_net,
                }
            )
        )
    return out
