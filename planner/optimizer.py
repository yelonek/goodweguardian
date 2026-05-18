"""Optymalizator DP: maksymalizacja cashflow PLN na horyzoncie godzin."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from economics import cashflow_pln_for_hour
from planner.battery import BatteryParams, apply_battery_step, battery_delta_from_net
from planner.config import (
    PLANNER_MAX_NET_KWH_PER_H,
    PLANNER_NET_STEP_KWH,
    PLANNER_SOC_STEP_PCT,
)
from planner.models import HourInputs, HourPlan

log = logging.getLogger("planner")


def _net_actions() -> list[float]:
    step = max(0.05, float(PLANNER_NET_STEP_KWH))
    cap = max(step, float(PLANNER_MAX_NET_KWH_PER_H))
    actions: list[float] = []
    v = -cap
    while v <= cap + 1e-9:
        actions.append(round(v, 4))
        v += step
    return actions


def _soc_grid(params: BatteryParams) -> list[float]:
    step = max(1, int(PLANNER_SOC_STEP_PCT))
    lo = int(params.soc_min_pct)
    hi = int(params.soc_max_pct)
    return [float(x) for x in range(lo, hi + 1, step)]


@dataclass
class OptimizeResult:
    hours: list[HourPlan]
    total_cashflow_pln: float
    soc_trajectory_pct: list[float]


def optimize_horizon(
    hours_in: list[HourInputs],
    *,
    soc_start_pct: float,
    params: BatteryParams | None = None,
) -> OptimizeResult:
    """
    Dynamic programming: stan = dyskretny SOC, akcja = target net_kwh.
    Maksymalizuje sumę cashflow_pln_for_hour.
    """
    bp = params or BatteryParams()
    actions = _net_actions()
    soc_states = _soc_grid(bp)
    if not hours_in:
        return OptimizeResult(hours=[], total_cashflow_pln=0.0, soc_trajectory_pct=[soc_start_pct])

    # start_idx: najbliższy dyskretny SOC
    start_idx = min(range(len(soc_states)), key=lambda i: abs(soc_states[i] - soc_start_pct))
    n_h = len(hours_in)
    n_s = len(soc_states)

    # dp[h][s] = (total_cf, prev_s, action_net)
    neg_inf = -1e30
    dp: list[list[float]] = [[neg_inf] * n_s for _ in range(n_h + 1)]
    back: list[list[tuple[int, float] | None]] = [[None] * n_s for _ in range(n_h + 1)]
    dp[0][start_idx] = 0.0

    for h_idx, hin in enumerate(hours_in):
        for s_idx, soc in enumerate(soc_states):
            base = dp[h_idx][s_idx]
            if base <= neg_inf / 2:
                continue
            for net in actions:
                bd = battery_delta_from_net(
                    pv_kwh=hin.pv_kwh, load_kwh=hin.load_kwh, net_kwh=net
                )
                soc_new = apply_battery_step(soc, bd, bp)
                if soc_new is None:
                    continue
                cf = cashflow_pln_for_hour(
                    net,
                    rce_pln_per_kwh=hin.export_pln_per_kwh,
                    import_pln_per_kwh=hin.import_pln_per_kwh,
                )
                new_idx = min(range(n_s), key=lambda i: abs(soc_states[i] - soc_new))
                total = base + cf
                if total > dp[h_idx + 1][new_idx]:
                    dp[h_idx + 1][new_idx] = total
                    back[h_idx + 1][new_idx] = (s_idx, net)

    # wybierz najlepszy koniec
    end_idx = max(range(n_s), key=lambda i: dp[n_h][i])
    if dp[n_h][end_idx] <= neg_inf / 2:
        log.warning("optimizer: brak ścieżki — fallback neutralny")
        return _fallback_neutral(hours_in, soc_start_pct, bp)

    # odtwórz ścieżkę (indeksy SOC + net)
    steps: list[tuple[int, float]] = []
    cur = end_idx
    for h_idx in range(n_h, 0, -1):
        prev = back[h_idx][cur]
        if prev is None:
            break
        p_idx, net = prev
        steps.append((p_idx, net))
        cur = p_idx
    steps.reverse()

    plans: list[HourPlan] = []
    traj: list[float] = [soc_states[start_idx]]
    total_cf = 0.0
    for hin, (s_idx, net) in zip(hours_in, steps, strict=False):
        soc = soc_states[s_idx]
        bd = battery_delta_from_net(pv_kwh=hin.pv_kwh, load_kwh=hin.load_kwh, net_kwh=net)
        soc_new = apply_battery_step(soc, bd, bp)
        if soc_new is None:
            soc_new = soc
        cf = cashflow_pln_for_hour(
            net,
            rce_pln_per_kwh=hin.export_pln_per_kwh,
            import_pln_per_kwh=hin.import_pln_per_kwh,
        )
        total_cf += cf
        plans.append(
            HourPlan(
                date=hin.date,
                hour=hin.hour,
                target_net_kwh=net,
                expected_cashflow_pln=cf,
                soc_start_pct=soc,
                soc_end_pct=soc_new,
                battery_delta_kwh=bd,
            )
        )
        traj.append(soc_new)

    return OptimizeResult(hours=plans, total_cashflow_pln=dp[n_h][end_idx], soc_trajectory_pct=traj)


def _fallback_neutral(
    hours_in: list[HourInputs],
    soc_start_pct: float,
    bp: BatteryParams,
) -> OptimizeResult:
    plans: list[HourPlan] = []
    soc = soc_start_pct
    total = 0.0
    traj = [soc]
    for hin in hours_in:
        net = 0.0
        bd = battery_delta_from_net(pv_kwh=hin.pv_kwh, load_kwh=hin.load_kwh, net_kwh=net)
        soc_new = apply_battery_step(soc, bd, bp) or soc
        cf = cashflow_pln_for_hour(
            net,
            rce_pln_per_kwh=hin.export_pln_per_kwh,
            import_pln_per_kwh=hin.import_pln_per_kwh,
        )
        total += cf
        plans.append(
            HourPlan(
                date=hin.date,
                hour=hin.hour,
                target_net_kwh=net,
                expected_cashflow_pln=cf,
                soc_start_pct=soc,
                soc_end_pct=soc_new,
                battery_delta_kwh=bd,
            )
        )
        soc = soc_new
        traj.append(soc)
    return OptimizeResult(hours=plans, total_cashflow_pln=total, soc_trajectory_pct=traj)
