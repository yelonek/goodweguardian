"""API /api/ev-charging/plan — remaining_kwh → cel dnia."""

import pytest
from fastapi.testclient import TestClient

from ev_charging_plan import EvChargingPlan
from ev_charging_store import read_declaration


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr("guardian_config.GUARDIAN_API_KEY", "secret-key")
    decl_path = tmp_path / "ev.json"
    monkeypatch.setattr("ev_charging_store.EV_CHARGING_DECLARATION_PATH", decl_path)

    import guardian_dashboard

    monkeypatch.setattr(guardian_dashboard.guardian_cfg, "GUARDIAN_API_KEY", "secret-key")
    monkeypatch.setattr(
        guardian_dashboard, "current_delivered_ev_kwh", lambda *_a, **_k: 8.0
    )
    monkeypatch.setattr(
        guardian_dashboard,
        "_replan_rolling_after_ev_change",
        lambda: {"replanned": False, "reason": "test"},
    )

    def fake_active_plan(path=None):
        decl = read_declaration()
        if decl is None:
            return EvChargingPlan(date="2026-09-01", delivered_kwh=8.0, remaining_kwh=0.0)
        rem = max(0.0, float(decl.target_kwh) - 8.0)
        return EvChargingPlan(
            date=decl.date,
            declaration=decl,
            delivered_kwh=8.0,
            remaining_kwh=rem,
        )

    monkeypatch.setattr(guardian_dashboard, "active_plan", fake_active_plan)
    return TestClient(guardian_dashboard.app)


def _auth() -> dict[str, str]:
    return {"X-Guardian-Api-Key": "secret-key"}


def test_put_remaining_kwh_adds_already_delivered(client: TestClient) -> None:
    r = client.put(
        "/api/ev-charging/plan",
        headers=_auth(),
        json={"remaining_kwh": 10.0, "max_power_kw": 3.0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["declaration"]["target_kwh"] == pytest.approx(18.0)
    assert body["declaration"]["max_power_kw"] == pytest.approx(3.0)
    assert body["delivered_kwh"] == pytest.approx(8.0)
    assert body["remaining_kwh"] == pytest.approx(10.0)


def test_put_default_power_is_3_kw(client: TestClient) -> None:
    r = client.put(
        "/api/ev-charging/plan",
        headers=_auth(),
        json={"remaining_kwh": 1.0},
    )
    assert r.status_code == 200
    assert r.json()["declaration"]["max_power_kw"] == pytest.approx(3.0)


def test_put_legacy_target_kwh_is_daily_total(client: TestClient) -> None:
    r = client.put(
        "/api/ev-charging/plan",
        headers=_auth(),
        json={"target_kwh": 15.0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["declaration"]["target_kwh"] == pytest.approx(15.0)
    assert body["remaining_kwh"] == pytest.approx(7.0)


def test_put_remaining_wins_over_target_kwh(client: TestClient) -> None:
    r = client.put(
        "/api/ev-charging/plan",
        headers=_auth(),
        json={"remaining_kwh": 10.0, "target_kwh": 15.0},
    )
    assert r.status_code == 200
    assert r.json()["declaration"]["target_kwh"] == pytest.approx(18.0)


def test_put_requires_remaining_or_target(client: TestClient) -> None:
    r = client.put(
        "/api/ev-charging/plan",
        headers=_auth(),
        json={"preferred_start_hour": 10},
    )
    assert r.status_code == 422
