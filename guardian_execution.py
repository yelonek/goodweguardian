"""Egzekucja ``exec_mode`` z planera (§13 PLANNING_SYSTEM.md)."""

from __future__ import annotations

from guardian_settings import get_settings
from guardian_logic import (
    BalanceInputs,
    WatchdogConfig,
    WatchdogDecision,
    _battery_pct_from_w,
    _deficit_recovery_decision,
    _discharge_cap_w,
    _neutral_decision,
    _steady_decision,
    battery_discharge_cap_w,
    clamp_discharge_to_soc_cap,
    compute_export_profit_pace_w,
    decide_flappy_relative,
    decide_soc_defenses,
)
from planner.models import ExecMode, HourPolicyParams, HourPolicyRow

# Kontrakt §13 — nie strojenie UI.
EXEC_STEADY_PCT = 1
IMPORT_GRID_SOC_PCT = 10
EXEC_MIN_ACTIVE_CHARGE_PCT = 2
EXEC_MIN_ACTIVE_DISCHARGE_PCT = 2
EXEC_EARLY_INTERVENTION_KW = 1.0
EXEC_NET_TOLERANCE_KWH = 0.03


def _params_pct(params: HourPolicyParams, field: str, *, minimum: int, default: int) -> int:
    raw = getattr(params, field, None)
    if raw is None:
        return max(minimum, default)
    return max(minimum, min(100, int(raw)))


def _charge_power_decision(
    inp: BalanceInputs,
    *,
    charge_w: float,
    reason: str,
    target_soc: float = 100.0,
) -> WatchdogDecision:
    charge_w = max(0.0, min(float(inp.p_battery_w), float(charge_w)))
    if charge_w < 0.5 * float(inp.watts_per_percent):
        return _neutral_decision(f"{reason}_no_power")
    pct = max(
        EXEC_MIN_ACTIVE_CHARGE_PCT,
        min(100, _battery_pct_from_w(charge_w, inp.watts_per_percent)),
    )
    return _steady_decision(
        power_pct=-pct,
        mode="charge",
        reason=reason,
        time_to_end_s=inp.time_to_end_s,
        slot_soc_pct=max(10, min(100, int(round(target_soc)))),
    )


def _hold_export_cap(
    inp: BalanceInputs,
    params: HourPolicyParams,
) -> WatchdogDecision | None:
    if params.max_additional_export_kwh is None:
        return None
    target = float(params.target_net_kwh)
    if float(inp.remaining_kwh) < target - EXEC_NET_TOLERANCE_KWH:
        return None
    surplus_w = max(0.0, float(inp.pv_w) - float(inp.consumption_w))
    if surplus_w <= 0.0:
        return _neutral_decision("planned_export_limit_reached")
    return _charge_power_decision(
        inp,
        charge_w=surplus_w,
        reason="planned_export_limit_pv_soak",
        target_soc=float(params.soc_end_pct),
    )


def _exec_export_profit(
    inp: BalanceInputs,
    params: HourPolicyParams,
    cfg: WatchdogConfig,
) -> WatchdogDecision:
    capped = _hold_export_cap(inp, params)
    if capped is not None:
        return capped

    floor = float(
        params.soc_floor_pct
        if params.soc_floor_pct is not None
        else get_settings().planner_soc_min_pct
    )
    if float(inp.soc_pct) <= floor + 0.5:
        return _steady_decision(
            power_pct=EXEC_STEADY_PCT,
            mode="discharge",
            reason="export_profit_soc_floor",
            time_to_end_s=inp.time_to_end_s,
        )

    plan_pct = _params_pct(
        params,
        "discharge_pct",
        minimum=EXEC_MIN_ACTIVE_DISCHARGE_PCT,
        default=EXEC_MIN_ACTIVE_DISCHARGE_PCT,
    )
    plan_max_w = plan_pct * inp.watts_per_percent
    full_max_w = min(
        _discharge_cap_w(inp.p_inverter_w, inp.pv_w, inp.p_battery_w),
        float(inp.p_battery_w),
        plan_max_w,
    )
    taper_cap = battery_discharge_cap_w(inp, cfg, full_max_w=full_max_w)
    taper_w = 0.0 if taper_cap is None else taper_cap
    target_w = compute_export_profit_pace_w(
        inp,
        plan_discharge_pct=plan_pct,
        min_discharge_pct=EXEC_MIN_ACTIVE_DISCHARGE_PCT,
        taper_max_w=taper_w,
    )
    if params.max_additional_export_kwh is not None:
        hours_left = max(1.0 / 3600.0, float(inp.time_to_end_s) / 3600.0)
        grid_pace_w = (
            max(0.0, float(params.target_net_kwh) - float(inp.remaining_kwh))
            / hours_left
            * 1000.0
        )
        battery_pace_w = grid_pace_w - (
            float(inp.pv_w) - float(inp.consumption_w)
        )
        target_w = min(target_w, max(0.0, battery_pace_w))
    if target_w <= 0.0:
        return _steady_decision(
            power_pct=EXEC_STEADY_PCT,
            mode="discharge",
            reason="export_profit_soc_floor",
            time_to_end_s=inp.time_to_end_s,
        )

    pct = max(
        EXEC_MIN_ACTIVE_DISCHARGE_PCT,
        min(plan_pct, _battery_pct_from_w(target_w, inp.watts_per_percent)),
    )
    return _steady_decision(
        power_pct=pct,
        mode="discharge",
        reason="export_profit_pace",
        time_to_end_s=inp.time_to_end_s,
    )


def _exec_export_pv_surplus(
    inp: BalanceInputs,
    params: HourPolicyParams,
    cfg: WatchdogConfig,
) -> WatchdogDecision:
    if float(inp.remaining_kwh) < 0.0:
        return _deficit_recovery_decision(inp, cfg)
    capped = _hold_export_cap(inp, params)
    if capped is not None:
        return capped
    return _steady_decision(
        power_pct=EXEC_STEADY_PCT,
        mode="discharge",
        reason="export_pv_surplus",
        time_to_end_s=inp.time_to_end_s,
    )


def _exec_import_grid(inp: BalanceInputs) -> WatchdogDecision:
    return _steady_decision(
        power_pct=-EXEC_STEADY_PCT,
        mode="charge",
        reason="import_grid",
        time_to_end_s=inp.time_to_end_s,
        slot_soc_pct=IMPORT_GRID_SOC_PCT,
    )


def _exec_charge_grid(
    inp: BalanceInputs,
    params: HourPolicyParams,
) -> WatchdogDecision:
    target_soc = float(params.target_soc_pct if params.target_soc_pct is not None else params.soc_end_pct)
    if float(inp.soc_pct) >= target_soc - 0.5:
        return _neutral_decision("charge_grid_target_reached")
    if float(params.planned_charge_kwh) > 0.0:
        hours_left = max(1.0 / 3600.0, float(inp.time_to_end_s) / 3600.0)
        planned_w = float(params.planned_charge_kwh) / hours_left * 1000.0
        # AC CHARGE zjada moc z szyny: tylko nadwyżka PV−load jest „darmowa”.
        # Całe PV jako cap powodowało import domu z sieci (13:31 2026-09-18).
        pv_surplus_w = max(0.0, float(inp.pv_w) - float(inp.consumption_w))
        grid_w = (
            float(params.grid_charge_budget_kwh) / hours_left * 1000.0
            if params.allow_grid_charge
            else 0.0
        )
        source_cap_w = pv_surplus_w + max(0.0, grid_w)
        return _charge_power_decision(
            inp,
            charge_w=min(planned_w, source_cap_w),
            reason=(
                "charge_grid_budgeted"
                if params.allow_grid_charge
                else "charge_pv_only"
            ),
            target_soc=target_soc,
        )
    pct = _params_pct(
        params,
        "charge_pct",
        minimum=EXEC_MIN_ACTIVE_CHARGE_PCT,
        default=EXEC_MIN_ACTIVE_CHARGE_PCT,
    )
    if not params.allow_grid_charge and pct > EXEC_STEADY_PCT:
        pct = EXEC_STEADY_PCT
    return _steady_decision(
        power_pct=-pct,
        mode="charge",
        reason="charge_grid",
        time_to_end_s=inp.time_to_end_s,
        slot_soc_pct=max(10, min(100, int(round(target_soc)))),
    )


def _exec_neutral(
    inp: BalanceInputs,
    params: HourPolicyParams,
    cfg: WatchdogConfig,
) -> WatchdogDecision:
    return decide_flappy_relative(
        inp,
        cfg=cfg,
        target_net_kwh=float(params.target_net_kwh),
        early_intervention_kw=EXEC_EARLY_INTERVENTION_KW,
        plan_battery_delta_kwh=float(params.battery_delta_kwh),
    )


_EXEC_HANDLERS = {
    "export_profit": lambda inp, row, cfg: _exec_export_profit(inp, row.params, cfg),
    "export_pv_surplus": lambda inp, row, cfg: _exec_export_pv_surplus(inp, row.params, cfg),
    "neutral": lambda inp, row, cfg: _exec_neutral(inp, row.params, cfg),
    "import_grid": lambda inp, row, cfg: _exec_import_grid(inp),
    "charge_pv": lambda inp, row, cfg: _exec_charge_grid(
        inp, row.params.model_copy(update={"allow_grid_charge": False})
    ),
    "charge_grid": lambda inp, row, cfg: _exec_charge_grid(inp, row.params),
}


def decide_plan_execution(
    inp: BalanceInputs,
    policy_row: HourPolicyRow,
    *,
    cfg: WatchdogConfig,
    minute_of_hour: int | None = None,
    hour_of_day: int | None = None,
    soc_full_defense_carryover: bool = False,
) -> WatchdogDecision:
    """
    Router ``exec_mode`` → strategia. ``inp.remaining_kwh`` = bilans licznika od :00.

    Obrony SOC przed trybem planera — z wyjątkami per ``exec_mode`` (§13).
    """
    soc = decide_soc_defenses(
        inp,
        cfg=cfg,
        minute_of_hour=minute_of_hour,
        hour_of_day=hour_of_day,
        soc_full_defense_carryover=soc_full_defense_carryover,
        exec_mode=policy_row.exec_mode,
        plan_battery_delta_kwh=float(policy_row.params.battery_delta_kwh),
    )
    if soc is not None:
        return clamp_discharge_to_soc_cap(soc, inp, cfg)

    mode: ExecMode = policy_row.exec_mode
    handler = _EXEC_HANDLERS.get(mode)
    if handler is None:
        return clamp_discharge_to_soc_cap(
            _neutral_decision(f"unknown_exec_mode:{mode}"), inp, cfg
        )

    decision = handler(inp, policy_row, cfg)
    return clamp_discharge_to_soc_cap(decision, inp, cfg)
