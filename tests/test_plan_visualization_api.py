"""API /api/plan/visualization — overview plan timeline."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from planner.models import (
    DailyPlan,
    HourInputs,
    HourPlan,
    ScenarioSeriesDetail,
    ScenariosDetail,
)
from planner.scenarios import QUANTILES, scenario_name


def _pricing_day(*, rce_by_hour: dict[int, float]) -> dict:
    hours = [
        {
            "hour": h,
            "import_pln_per_kwh": 0.59,
            "rce_pln_kwh": rce,
        }
        for h, rce in sorted(rce_by_hour.items())
    ]
    return {"source": "test", "hours": hours}


@pytest.fixture
def plan_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    from guardian_dashboard import app

    today = date.today()
    today_iso = today.isoformat()

    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    plan = DailyPlan(
        plan_id="viz-plan",
        local_date=today_iso,
        generated_at=datetime.now(UTC).isoformat(),
        timezone="Europe/Warsaw",
        horizon_start=f"{today_iso}T08:00:00",
        horizon_end=f"{today_iso}T19:00:00",
        soc_start_pct=50.0,
        soc_trajectory_pct=[50.0 + h * 0.5 for h in range(13)],
        expected_total_cashflow_pln=12.5,
        optimizer="stochastic_mpc_v1",
        inputs_snapshot={},
        hours=[
            HourPlan(
                date=today_iso,
                hour=h,
                target_net_kwh=1.0 if h == 10 else 0.0,
                expected_cashflow_pln=0.5 if h == 10 else 0.0,
                soc_start_pct=50.0,
                soc_end_pct=50.0 + h * 0.5,
                battery_delta_kwh=0.0,
            )
            for h in range(8, 20)
        ],
        scenarios_detail=ScenariosDetail(
            model="stochastic_mpc_recourse",
            expected_cashflow_pln=12.5,
            soc_star_pct=[50.0 + i for i in range(13)],
            slots=[{"date": today_iso, "hour": h} for h in range(8, 20)],
            scenarios={
                scenario_name(qpv, qld): ScenarioSeriesDetail(
                    weight=1.0 / 25.0,
                    cashflow_pln=8.0 + qpv / 10.0,
                    soc_pct=[50.0 + i for i in range(13)],
                    net_kwh=[0.1 + qpv / 100.0] * 12,
                    cashflow_hour_pln=[(8.0 + qpv / 10.0) / 12] * 12,
                    charge_kwh=[0.2] * 12,
                    discharge_kwh=[0.1] * 12,
                )
                for qpv in QUANTILES
                for qld in QUANTILES
            },
        ),
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

    def pricing(local_date):
        if str(local_date) == today_iso:
            return _pricing_day(rce_by_hour={h: 0.25 for h in range(24)})
        return _pricing_day(rce_by_hour={h: 0.70 for h in range(24)})

    monkeypatch.setattr("guardian_dashboard.pricing_day_breakdown", pricing)
    monkeypatch.setattr("guardian_dashboard._pricing_for_day_quiet", pricing)
    monkeypatch.setattr(
        "guardian_dashboard.fetch_hourly_pv_forecast_with_history",
        lambda **_: {"hours": [{"date": today_iso, "hour": h, "pv_kw": 1.0} for h in range(24)]},
    )
    monkeypatch.setattr(
        "guardian_dashboard._telemetry_hourly_load_pv_actuals",
        lambda _d: ({}, {}),
    )
    monkeypatch.setattr("guardian_dashboard.twc_enabled", lambda: False)

    return TestClient(app)


def test_plan_visualization_24_hours_per_day(plan_client: TestClient) -> None:
    r = plan_client.get("/api/plan/visualization")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert len(body["days"]) == 2
    assert len(body["days"][0]["hours"]) == 24
    assert len(body["days"][1]["hours"]) == 24
    assert body["meta"]["expected_cashflow_pln"] == pytest.approx(12.5)

    today_hours = body["days"][0]["hours"]
    h10 = next(h for h in today_hours if h["hour"] == 10)
    assert h10["exec_mode"] is not None
    assert h10["target_net_kwh"] is not None

    detail = body["scenarios_detail"]
    assert detail is not None
    assert detail["model"] == "stochastic_mpc_recourse"
    assert len(detail["soc_star_pct"]) == 13
    assert len(detail["scenarios"]) == 25
    assert "pv50_ld50" in detail["scenarios"]
    assert "pv10_ld90" in detail["scenarios"]
    assert len(detail["scenarios"]["pv50_ld50"]["soc_pct"]) == 13
    assert len(detail["scenarios"]["pv50_ld50"]["net_kwh"]) == 12
    assert len(detail["scenarios"]["pv50_ld50"]["charge_kwh"]) == 12
    assert len(detail["scenarios"]["pv50_ld50"]["discharge_kwh"]) == 12
    assert body["meta"]["optimizer"] == "stochastic_mpc_v1"


def test_plan_visualization_unavailable_without_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    from guardian_dashboard import app

    monkeypatch.setattr("guardian_dashboard.load_latest_plan", lambda: None)
    client = TestClient(app)
    r = client.get("/api/plan/visualization")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert "plan_latest" in body["reason"]
