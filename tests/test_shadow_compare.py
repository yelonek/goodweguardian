from __future__ import annotations

import json
from pathlib import Path

import pytest

from planner.models import DailyPlan, HourInputs, HourPlan
from planner.shadow_compare import compare_plans, load_comparison, save_shadow_artifacts


def _inputs() -> list[HourInputs]:
    return [
        HourInputs(
            date="2026-09-18",
            hour=15,
            load_kwh=0.5,
            pv_kwh=0.0,
            import_pln_per_kwh=1.10,
            export_pln_per_kwh=0.10,
        ),
        HourInputs(
            date="2026-09-18",
            hour=19,
            load_kwh=0.5,
            pv_kwh=0.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=2.50,
        ),
    ]


def _plan(plan_id: str, optimizer: str, *, cash: float, charge: float) -> DailyPlan:
    snapshot = {"hour_inputs": [h.model_dump(mode="json") for h in _inputs()]}
    return DailyPlan(
        plan_id=plan_id,
        local_date="2026-09-18",
        generated_at="2026-09-18T12:00:00+00:00",
        timezone="Europe/Warsaw",
        horizon_start="2026-09-18T15:00:00",
        horizon_end="2026-09-18T19:00:00",
        soc_start_pct=10.0,
        expected_total_cashflow_pln=cash,
        optimizer=optimizer,
        inputs_snapshot=snapshot,
        hours=[
            HourPlan(
                date="2026-09-18",
                hour=15,
                target_net_kwh=-charge,
                expected_cashflow_pln=-charge * 1.10,
                soc_start_pct=10.0,
                soc_end_pct=50.0,
                battery_delta_kwh=charge,
                planned_charge_kwh=charge,
                planned_discharge_kwh=0.0,
            ),
            HourPlan(
                date="2026-09-18",
                hour=19,
                target_net_kwh=charge * 0.9,
                expected_cashflow_pln=charge * 0.9 * 2.50,
                soc_start_pct=50.0,
                soc_end_pct=10.0,
                battery_delta_kwh=-charge * 0.9,
                planned_charge_kwh=0.0,
                planned_discharge_kwh=charge * 0.9,
            ),
        ],
    )


def test_compare_plans_uses_identical_snapshot_and_reports_delta() -> None:
    primary = _plan("legacy", "shared", cash=1.0, charge=1.0)
    candidate = _plan("mpc", "stochastic", cash=2.5, charge=2.0)

    out = compare_plans(primary, candidate, _inputs())

    assert out["same_inputs"] is True
    assert out["delta"]["expected_cashflow_pln"] == pytest.approx(1.5)
    assert out["candidate"]["first_decision"]["planned_charge_kwh"] == 2.0
    assert out["candidate"]["night_charge_export_cycles"] == 0


def test_shadow_artifacts_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import planner.shadow_compare as mod

    shadow = tmp_path / "shadow.json"
    comparison = tmp_path / "comparison.json"
    monkeypatch.setattr(mod, "PLANNER_SHADOW_PLAN_PATH", shadow)
    monkeypatch.setattr(mod, "PLANNER_SHADOW_COMPARISON_PATH", comparison)
    candidate = _plan("mpc", "stochastic", cash=2.5, charge=2.0)
    payload = {"schema_version": 1, "candidate_plan_id": "mpc"}

    save_shadow_artifacts(candidate, payload)

    assert json.loads(shadow.read_text())["plan_id"] == "mpc"
    assert load_comparison() == payload


def test_compare_cli_prints_latest_artifact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import planner.shadow_compare as mod
    from planner.__main__ import main

    monkeypatch.setattr(
        mod,
        "load_comparison",
        lambda: {"schema_version": 1, "candidate_plan_id": "mpc"},
    )
    assert main(["compare"]) == 0
    assert '"candidate_plan_id": "mpc"' in capsys.readouterr().out
