"""API /api/ecoslots (odczyt + zapis slotów 1–3)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr("guardian_config.GUARDIAN_API_KEY", "secret-key")
    monkeypatch.setattr("guardian_config.INVERTER_IP", "192.168.1.10")
    monkeypatch.setattr("guardian_config.ECO_SLOT_BALANCING", 4)
    snap_path = tmp_path / "ecoslots_snapshot.json"
    monkeypatch.setattr("ecoslot_service.ECOSLOTS_SNAPSHOT_PATH", snap_path)
    from ecoslot_service import build_ecoslots_payload, save_ecoslots_snapshot
    from types import SimpleNamespace
    from datetime import datetime

    save_ecoslots_snapshot(
        build_ecoslots_payload(
            {
                "eco_mode_1": SimpleNamespace(
                    start_h=8, start_m=0, end_h=12, end_m=0, power=-10,
                    days="Mon-Sun", soc=100, on_off=-2,
                ),
                "eco_mode_2": None,
                "eco_mode_3": None,
                "eco_mode_4": None,
            },
            now=datetime(2026, 6, 3, 12, 0),
            source="runner",
            supported_ids={"eco_mode_1"},
        )
    )
    from guardian_dashboard import app

    return TestClient(app)


def _fake_slot(**kwargs):
    defaults = dict(
        start_h=8,
        start_m=0,
        end_h=12,
        end_m=0,
        power=-50,
        days="Mon-Fri",
        soc=100,
        on_off=-2,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture
def mock_inverter(monkeypatch):
    inv = MagicMock()
    inv.settings.return_value = [
        SimpleNamespace(id_=f"eco_mode_{i}", size_=12) for i in range(1, 5)
    ]
    inv.read_setting = AsyncMock(
        side_effect=lambda sid: _fake_slot(power=-10 if sid == "eco_mode_1" else 5)
    )
    inv.write_setting = AsyncMock()
    monkeypatch.setattr(
        "ecoslot_service.goodwe.connect", AsyncMock(return_value=inv)
    )
    return inv


def test_ecoslots_get_from_snapshot(client: TestClient) -> None:
    r = client.get("/api/ecoslots")
    assert r.status_code == 200
    body = r.json()
    assert body["balancing_slot_id"] == "eco_mode_4"
    assert body["source"] == "runner"
    assert body["slots"]["eco_mode_1"]["power_pct"] == -10


def test_ecoslots_get_live(client: TestClient, mock_inverter) -> None:
    r = client.get("/api/ecoslots?refresh=1")
    assert r.status_code == 200
    assert r.json()["source"] == "inverter"
    mock_inverter.write_setting.assert_not_called()


def test_ecoslots_put_requires_key(client: TestClient, mock_inverter) -> None:
    r = client.put(
        "/api/ecoslots/eco_mode_1",
        json={
            "start_h": 9,
            "start_m": 0,
            "end_h": 10,
            "end_m": 0,
            "power": -30,
            "enabled": True,
        },
    )
    assert r.status_code == 401


def test_ecoslots_put_balancing_slot_forbidden(
    client: TestClient, mock_inverter
) -> None:
    r = client.put(
        "/api/ecoslots/eco_mode_4",
        headers={"X-Guardian-Api-Key": "secret-key"},
        json={
            "start_h": 9,
            "start_m": 0,
            "end_h": 10,
            "end_m": 0,
            "power": 1,
            "enabled": True,
        },
    )
    assert r.status_code == 400


def test_ecoslots_put_soc_out_of_range(client: TestClient, mock_inverter) -> None:
    r = client.put(
        "/api/ecoslots/eco_mode_1",
        headers={"X-Guardian-Api-Key": "secret-key"},
        json={
            "start_h": 9,
            "start_m": 0,
            "end_h": 10,
            "end_m": 0,
            "power": -30,
            "soc": 5,
            "enabled": True,
        },
    )
    assert r.status_code == 422


def test_ecoslots_put_roundtrip(client: TestClient, mock_inverter) -> None:
    r = client.put(
        "/api/ecoslots/eco_mode_2",
        headers={"X-Guardian-Api-Key": "secret-key"},
        json={
            "start_h": 22,
            "start_m": 0,
            "end_h": 6,
            "end_m": 0,
            "power": -80,
            "days": "Mon-Sun",
            "soc": 90,
            "enabled": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["slot_id"] == "eco_mode_2"
    mock_inverter.write_setting.assert_called()
