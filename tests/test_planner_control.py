"""API /api/guardian/planner i egzekucja planu w Guardianie."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr("guardian_config.GUARDIAN_API_KEY", "secret-key")
    monkeypatch.setattr(
        "guardian_config.PLANNER_OVERRIDE_PATH", tmp_path / "planner_override.json"
    )
    from guardian_dashboard import app

    return TestClient(app)


def test_planner_execution_put_and_get_roundtrip(client: TestClient) -> None:
    r = client.put(
        "/api/guardian/planner",
        headers={"X-Guardian-Api-Key": "secret-key"},
        json={"planner_execution_enabled": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["planner_execution_enabled"] is True
    assert body["source"] == "override"

    r2 = client.get(
        "/api/guardian/planner", headers={"X-Guardian-Api-Key": "secret-key"}
    )
    assert r2.status_code == 200
    assert r2.json()["planner_execution_enabled"] is True


def test_plan_target_net_kwh_for_hour(tmp_path, monkeypatch) -> None:
    from datetime import date

    import planner.plan_store as ps_mod
    from planner.config import PLANNER_LATEST_PLAN_PATH, PLANNER_PLANS_DIR, PLANNER_PLANS_HISTORY_DIR
    from planner.models import DailyPlan, HourPlan
    from planner.plan_target import plan_target_net_kwh_for_hour
    from planner.plan_store import save_plan

    plans = tmp_path / "plans"
    hist = plans / "history"
    hist.mkdir(parents=True)
    monkeypatch.setattr(ps_mod, "PLANNER_PLANS_DIR", plans)
    monkeypatch.setattr(ps_mod, "PLANNER_PLANS_HISTORY_DIR", hist)
    monkeypatch.setattr(ps_mod, "PLANNER_LATEST_PLAN_PATH", plans / "plan_latest.json")

    d = "2026-06-07"
    plan = DailyPlan(
        plan_id="pid",
        local_date=d,
        generated_at="2026-06-07T08:00:00+00:00",
        timezone="Europe/Warsaw",
        horizon_start=f"{d}T10:00:00",
        horizon_end=f"{d}T18:00:00",
        soc_start_pct=50.0,
        expected_total_cashflow_pln=1.0,
        optimizer="test",
        inputs_snapshot={},
        hours=[
            HourPlan(
                date=d,
                hour=10,
                target_net_kwh=1.25,
                expected_cashflow_pln=0.5,
                soc_start_pct=50.0,
                soc_end_pct=55.0,
                battery_delta_kwh=0.0,
            )
        ],
    )
    save_plan(plan)

    assert plan_target_net_kwh_for_hour(date(2026, 6, 7), 10) == pytest.approx(1.25)
    assert plan_target_net_kwh_for_hour(date(2026, 6, 7), 11) is None
