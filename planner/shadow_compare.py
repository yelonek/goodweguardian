"""Odtwarzalny artefakt porównania legacy vs stochastic MPC na tych samych wejściach."""

from __future__ import annotations

import json
from pathlib import Path

from planner.config import (
    PLANNER_SHADOW_COMPARISON_PATH,
    PLANNER_SHADOW_PLAN_PATH,
)
from planner.models import DailyPlan, HourInputs, HourPlan
from planner.night_grid_policy import night_windows


def _action(row: HourPlan) -> str:
    charge = float(row.planned_charge_kwh or max(0.0, row.battery_delta_kwh))
    discharge = float(
        row.planned_discharge_kwh or max(0.0, -row.battery_delta_kwh)
    )
    if charge > 0.05:
        return "charge"
    if discharge > 0.05:
        return "discharge"
    if row.target_net_kwh > 0.05:
        return "export"
    if row.target_net_kwh < -0.05:
        return "import"
    return "hold"


def _mode_changes(plan: DailyPlan) -> int:
    actions = [_action(row) for row in plan.hours]
    return sum(a != b for a, b in zip(actions, actions[1:]))


def _expensive_import_kwh(
    plan: DailyPlan, hour_inputs: list[HourInputs]
) -> float:
    if not hour_inputs:
        return 0.0
    cheap = min(float(h.import_pln_per_kwh) for h in hour_inputs) + 0.02
    return sum(
        max(0.0, -float(row.target_net_kwh))
        for row, hin in zip(plan.hours, hour_inputs, strict=True)
        if float(hin.import_pln_per_kwh) > cheap
    )


def _night_cycles(plan: DailyPlan, hour_inputs: list[HourInputs]) -> int:
    cycles = 0
    for window in night_windows(hour_inputs):
        charged = False
        for h in window:
            row = plan.hours[h]
            charged = charged or _action(row) == "charge"
            if charged and float(row.target_net_kwh) > 0.05:
                cycles += 1
    return cycles


def compare_plans(
    primary: DailyPlan,
    candidate: DailyPlan,
    hour_inputs: list[HourInputs],
) -> dict:
    def metrics(plan: DailyPlan) -> dict:
        first = plan.hours[0] if plan.hours else None
        return {
            "optimizer": plan.optimizer,
            "expected_cashflow_pln": plan.expected_total_cashflow_pln,
            "first_decision": (
                {
                    "target_net_kwh": first.target_net_kwh,
                    "planned_charge_kwh": first.planned_charge_kwh,
                    "planned_discharge_kwh": first.planned_discharge_kwh,
                    "soc_end_pct": first.soc_end_pct,
                }
                if first is not None
                else None
            ),
            "mode_changes": _mode_changes(plan),
            "expensive_import_kwh": _expensive_import_kwh(plan, hour_inputs),
            "night_charge_export_cycles": _night_cycles(plan, hour_inputs),
        }

    old = metrics(primary)
    new = metrics(candidate)
    return {
        "schema_version": 1,
        "primary_plan_id": primary.plan_id,
        "candidate_plan_id": candidate.plan_id,
        "horizon_start": primary.horizon_start,
        "horizon_end": primary.horizon_end,
        "same_inputs": primary.inputs_snapshot == candidate.inputs_snapshot,
        "primary": old,
        "candidate": new,
        "delta": {
            "expected_cashflow_pln": (
                new["expected_cashflow_pln"] - old["expected_cashflow_pln"]
            ),
            "mode_changes": new["mode_changes"] - old["mode_changes"],
            "expensive_import_kwh": (
                new["expensive_import_kwh"] - old["expensive_import_kwh"]
            ),
            "night_charge_export_cycles": (
                new["night_charge_export_cycles"]
                - old["night_charge_export_cycles"]
            ),
        },
    }


def save_shadow_artifacts(candidate: DailyPlan, comparison: dict) -> None:
    PLANNER_SHADOW_PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLANNER_SHADOW_PLAN_PATH.write_text(
        json.dumps(candidate.model_dump(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    PLANNER_SHADOW_COMPARISON_PATH.write_text(
        json.dumps(comparison, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_comparison(path: Path | None = None) -> dict | None:
    target = path or PLANNER_SHADOW_COMPARISON_PATH
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))
