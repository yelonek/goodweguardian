"""API /api/pv-correction — panel korekty PV."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def pv_corr_client():
    from guardian_dashboard import app

    return TestClient(app)


def test_pv_correction_api_returns_payload(pv_corr_client: TestClient) -> None:
    fake = {
        "now": "2026-07-10T12:30:00",
        "date": "2026-07-10",
        "current_hour": 12,
        "correction": {"enabled": True, "alpha": 0.5, "k_intra": 0.8},
        "projections": {"final_plan_kwh": 2.0},
        "minute_series": [],
        "projection_curve": [],
        "clip_timeline": [],
        "today_hours": [],
    }
    with patch("guardian_dashboard._get_pv_correction_cached", return_value=fake):
        r = pv_corr_client.get("/api/pv-correction")
    assert r.status_code == 200
    body = r.json()
    assert body["current_hour"] == 12
    assert body["correction"]["k_intra"] == 0.8


def test_dashboard_ui_has_pv_correction_page(pv_corr_client: TestClient) -> None:
    r = pv_corr_client.get("/")
    assert r.status_code == 200
    assert 'id="page-pv-correction"' in r.text
    assert 'data-page="pv-correction"' in r.text
    js = pv_corr_client.get("/dashboard.js")
    assert "loadPvCorrection" in js.text
    assert '"pv-correction": loadPvCorrection' in js.text
