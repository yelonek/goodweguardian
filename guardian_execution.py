"""Egzekucja ``exec_mode`` z planera (§13 PLANNING_SYSTEM.md)."""

from __future__ import annotations

from guardian_config import (
    BATTERY_CAPACITY_KWH,
    EXEC_EARLY_INTERVENTION_KW,
    EXEC_MIN_ACTIVE_CHARGE_PCT,
    EXEC_MIN_ACTIVE_DISCHARGE_PCT,
    EXEC_STEADY_PCT,
    EXPORT_PROFIT_PACE_MARGIN,
    IMPORT_GRID_SOC_PCT,
    SOC_LOW_DISCHARGE_FALLBACK_W,
    SOC_LOW_DISCHARGE_MAX_W,
)
from guardian_logic import (
    BalanceInputs,
    WatchdogConfig,
    WatchdogDecision,
    _battery_pct_from_w,
    _deficit_recovery_decision,
    _neutral_decision,
    _steady_decision,
    compute_export_profit_pace_w,
    decide_flappy_relative,
    decide_soc_defenses,
)
from planner.models import ExecMode, HourPolicyParams, HourPolicyRow


def _params_pct(params: HourPolicyParams, field: str, *, minimum: int, default: int) -> int:
    raw = getattr(params, field, None)
    if raw is None:
        return max(minimum, default)
    return max(minimum, min(100, int(raw)))


def _export_profit_taper_max_w() -> float:
    cap = float(SOC_LOW_DISCHARGE_MAX_W)
    if cap > 0.0:
        return cap
    return max(0.0, float(SOC_LOW_DISCHARGE_FALLBACK_W))


def _exec_export_profit(
    inp: BalanceInputs,
    params: HourPolicyParams,
    cfg: WatchdogConfig,
) -> WatchdogDecision:
    floor = float(params.soc_floor_pct if params.soc_floor_pct is not None else cfg.soc_low_threshold_pct)
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
    target_w = compute_export_profit_pace_w(
        inp,
        floor_pct=floor,
        capacity_kwh=BATTERY_CAPACITY_KWH,
        pace_margin=EXPORT_PROFIT_PACE_MARGIN,
        taper_threshold_pct=float(cfg.soc_low_threshold_pct),
        taper_max_w=_export_profit_taper_max_w(),
        plan_discharge_pct=plan_pct,
        min_discharge_pct=EXEC_MIN_ACTIVE_DISCHARGE_PCT,
    )
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


def _exec_export_pv_surplus(inp: BalanceInputs, cfg: WatchdogConfig) -> WatchdogDecision:
    if float(inp.remaining_kwh) < 0.0:
        return _deficit_recovery_decision(inp, cfg)
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
    )


_EXEC_HANDLERS = {
    "export_profit": lambda inp, row, cfg: _exec_export_profit(inp, row.params, cfg),
    "export_pv_surplus": lambda inp, row, cfg: _exec_export_pv_surplus(inp, cfg),
    "neutral": lambda inp, row, cfg: _exec_neutral(inp, row.params, cfg),
    "import_grid": lambda inp, row, cfg: _exec_import_grid(inp),
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
    )
    if soc is not None:
        return soc

    mode: ExecMode = policy_row.exec_mode
    handler = _EXEC_HANDLERS.get(mode)
    if handler is None:
        return _neutral_decision(f"unknown_exec_mode:{mode}")

    decision = handler(inp, policy_row, cfg)
    return decision
