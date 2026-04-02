"""API /api/guardian/control."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr("guardian_config.GUARDIAN_API_KEY", "secret-key")
    monkeypatch.setattr(
        "guardian_config.GUARDIAN_CONTROL_OVERRIDE_PATH", tmp_path / "override.json"
    )
    from guardian_dashboard import app

    return TestClient(app)


def test_control_get_401_bad_key(client: TestClient) -> None:
    r = client.get("/api/guardian/control", headers={"X-Guardian-Api-Key": "wrong"})
    assert r.status_code == 401


def test_control_get_503_when_key_unset(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("guardian_config.GUARDIAN_API_KEY", "")
    monkeypatch.setattr(
        "guardian_config.GUARDIAN_CONTROL_OVERRIDE_PATH", tmp_path / "override.json"
    )
    import guardian_dashboard

    monkeypatch.setattr(guardian_dashboard.guardian_cfg, "GUARDIAN_API_KEY", "")
    c = TestClient(guardian_dashboard.app)
    r = c.get("/api/guardian/control", headers={"X-Guardian-Api-Key": "any"})
    assert r.status_code == 503


def test_control_put_and_get_roundtrip(client: TestClient) -> None:
    r = client.put(
        "/api/guardian/control",
        headers={"X-Guardian-Api-Key": "secret-key"},
        json={"control_enabled": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["control_enabled"] is False
    assert body["source"] == "override"

    r2 = client.get(
        "/api/guardian/control", headers={"X-Guardian-Api-Key": "secret-key"}
    )
    assert r2.status_code == 200
    assert r2.json()["control_enabled"] is False
