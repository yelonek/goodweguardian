"""Dashboard: policy z planera w prognozie łączonej."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from planner.models import DailyPlan, HourInputs, HourPlan


def test_combined_forecast_includes_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from guardian_dashboard import _combined_forecast_payload

    today = date.today()
    today_iso = today.isoformat()

    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    export_hour = 14
    plan = DailyPlan(
        plan_id="dash-plan",
        local_date=today_iso,
        generated_at=datetime.now(UTC).isoformat(),
        timezone="Europe/Warsaw",
        horizon_start=f"{today_iso}T08:00:00",
        horizon_end=f"{today_iso}T19:00:00",
        soc_start_pct=50.0,
        expected_total_cashflow_pln=0.0,
        optimizer="test",
        inputs_snapshot={},
        hours=[
            HourPlan(
                date=today_iso,
                hour=h,
                target_net_kwh=1.0 if h == export_hour else 0.0,
                expected_cashflow_pln=0.0,
                soc_start_pct=50.0,
                soc_end_pct=51.0,
                battery_delta_kwh=0.0,
            )
            for h in range(8, 20)
        ],
    )
    latest = plans_dir / "plan_latest.json"
    latest.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    import planner.plan_store as ps_mod
    import planner.policy_output as po_mod

    monkeypatch.setattr(ps_mod, "PLANNER_LATEST_PLAN_PATH", latest)
    monkeypatch.setattr(po_mod, "PLANNER_OUTPUT_PATH", state_dir / "planner_output.json")

    from planner.policy_output import build_policy_artifact, save_policy_artifact

    save_policy_artifact(
        build_policy_artifact(
            plan,
            [
                HourInputs(
                    date=today_iso,
                    hour=h,
                    load_kwh=1.0,
                    pv_kwh=2.0,
                    import_pln_per_kwh=1.0,
                    export_pln_per_kwh=0.4,
                )
                for h in range(8, 20)
            ],
        )
    )

    payload = _combined_forecast_payload()
    with_policy = [r for r in payload["rows"] if r.get("policy") is not None]
    assert with_policy, "expected at least one row with policy"
    row = next(r for r in with_policy if r["policy"] == "hold_export")
    assert row["policy_label"] == "eksport PV"
    assert row["policy_battery_delta_kwh"] == pytest.approx(0.0)
