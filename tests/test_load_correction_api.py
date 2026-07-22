"""API /api/load-correction — panel korekty load."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def load_corr_client():
    from guardian_dashboard import app

    return TestClient(app)


def test_load_correction_api_returns_payload(load_corr_client: TestClient) -> None:
    fake = {
        "now": "2026-07-22T11:30:00",
        "date": "2026-07-22",
        "current_hour": 11,
        "correction": {
            "enabled": True,
            "alpha": 0.5,
            "k_intra": 1.35,
            "applied": True,
            "plan_method": "k_intra_rate_blend",
        },
        "projections": {"final_plan_kwh": 5.8, "forecast_full_hour_kwh": 3.14},
        "minute_series": [],
        "projection_curve": [],
        "today_hours": [],
        "remainder_bands": {"load_planner_active": True},
    }
    with patch("guardian_dashboard._get_load_correction_cached", return_value=fake):
        r = load_corr_client.get("/api/load-correction")
    assert r.status_code == 200
    body = r.json()
    assert body["current_hour"] == 11
    assert body["correction"]["k_intra"] == 1.35
    assert body["remainder_bands"]["load_planner_active"] is True


def test_dashboard_ui_has_load_correction_page(load_corr_client: TestClient) -> None:
    r = load_corr_client.get("/")
    assert r.status_code == 200
    assert 'id="page-load-correction"' in r.text
    assert 'data-page="load-correction"' in r.text
    js = load_corr_client.get("/dashboard.js")
    assert "loadLoadCorrection" in js.text
    assert '"load-correction": loadLoadCorrection' in js.text
    assert "renderLoadCorrectionBands" in js.text
    assert 'id="loadCorrectionBands"' in r.text


def test_planner_load_milp_snapshot_mid_hour() -> None:
    from datetime import datetime

    from planner.load_planner_display import planner_load_milp_snapshot

    now = datetime(2026, 7, 22, 11, 30, 0)
    snap = planner_load_milp_snapshot(
        now=now,
        date_iso="2026-07-22",
        hour=11,
        load_p50_kwh=3.14,
        load_p25_kwh=2.5,
        load_p75_kwh=4.0,
        load_meta={
            "enabled": True,
            "band_narrow_enabled": True,
            "alpha": 0.5,
            "a_so_far_kwh": 3.14,
            "recent_kw": 5.966,
            "telemetry_samples": 30,
            "recent_samples": 15,
        },
    )
    assert snap is not None
    assert snap["load_planner_active"] is True
    assert snap["load_planner_remainder_p50_kwh"] > 1.0
    assert snap["load_planner_full_p50_kwh"] > 3.14
