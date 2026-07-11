"""Alert: aktywny inny eco slot nadpisuje plan Guardiana."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import ecoslot_service as svc


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr("guardian_config.GUARDIAN_API_KEY", "secret-key")
    monkeypatch.setattr("guardian_config.LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr("guardian_config.TELEMETRY_DIR", tmp_path / "telemetry")
    from guardian_dashboard import app

    return TestClient(app)


def _snapshot_with_active_slot_1(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(svc, "ECOSLOTS_SNAPSHOT_PATH", tmp_path / "ecoslots_snapshot.json")
    monkeypatch.setattr(svc, "INVERTER_IP", "192.168.1.10")
    monkeypatch.setattr(svc, "ECO_SLOT_BALANCING", 4)
    now = datetime(2026, 7, 11, 10, 30)
    svc.save_ecoslots_snapshot(
        svc.build_ecoslots_payload(
            {
                "eco_mode_1": SimpleNamespace(
                    start_h=8, start_m=0, end_h=12, end_m=0, power=-60,
                    days="Mon-Sun", soc=100, on_off=-2,
                ),
                "eco_mode_2": None,
                "eco_mode_3": None,
                "eco_mode_4": SimpleNamespace(
                    start_h=10, start_m=30, end_h=10, end_m=31, power=1,
                    days="Mon-Sun", soc=100, on_off=-2,
                ),
            },
            now=now,
            source="runner",
            supported_ids={"eco_mode_1", "eco_mode_4"},
        )
    )


def test_active_override_slots_excludes_balancing_slot(tmp_path, monkeypatch) -> None:
    _snapshot_with_active_slot_1(tmp_path, monkeypatch)
    snap = svc.load_ecoslots_payload_from_snapshot()
    assert snap is not None
    slots = svc.active_override_slots(snap)
    assert len(slots) == 1
    assert slots[0]["slot_id"] == "eco_mode_1"
    assert slots[0]["power_pct"] == -60
    assert slots[0]["end"] == "12:00"


def test_ecoslot_override_alert_from_snapshot(tmp_path, monkeypatch) -> None:
    _snapshot_with_active_slot_1(tmp_path, monkeypatch)
    alert = svc.ecoslot_override_alert_payload(runner_other_eco=False)
    assert alert["active"] is True
    assert len(alert["slots"]) == 1


def test_ecoslot_override_alert_from_runner_flag_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(svc, "ECOSLOTS_SNAPSHOT_PATH", tmp_path / "missing.json")
    alert = svc.ecoslot_override_alert_payload(runner_other_eco=True)
    assert alert["active"] is True
    assert alert["slots"] == []


def test_ecoslot_override_inactive_when_no_slots(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(svc, "ECOSLOTS_SNAPSHOT_PATH", tmp_path / "missing.json")
    alert = svc.ecoslot_override_alert_payload(runner_other_eco=False)
    assert alert["active"] is False


def test_api_status_includes_ecoslot_override(client, tmp_path, monkeypatch) -> None:
    _snapshot_with_active_slot_1(tmp_path, monkeypatch)
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["ecoslot_override"]["active"] is True
    assert body["ecoslot_override"]["slots"][0]["slot_label"] == "slot 1"
