"""Dashboard musi serwować dashboard_ui.html + dashboard.js, nie legacy inline HTML."""

from fastapi.testclient import TestClient


def test_index_uses_external_ui_file() -> None:
    from guardian_dashboard import app

    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'id="mainNav"' in body
    assert 'id="page-overview"' in body
    assert 'src="/dashboard.js"' in body
    assert "<script>" not in body
    assert "advanced-panel" not in body
    assert "Current state" not in body

    js = client.get("/dashboard.js")
    assert js.status_code == 200
    assert "application/javascript" in js.headers.get("content-type", "")
    assert "function navigate(" in js.text
    assert "class=\"tag\">ACTIVE" in js.text
    assert "Eco slots (eco_mode_1–4)" in body
    assert "function ecoSlotIdsInOrder(" in js.text
    assert "eco-slot-readonly" in js.text
    assert "tag-guardian" in js.text
    assert "function renderEcoSlotCard(" in js.text
    assert "Plan włączony — slot balansujący tylko do odczytu" in js.text
    assert "balancing: sid === balancingId" in js.text
    assert 'id="forecastPvDayChart"' in body
    assert 'id="forecastLoadResidualChart"' in body
    assert 'id="forecastBalanceSocChart"' in body
    assert "function renderForecastDayCharts(" in js.text
    assert "function renderForecastLoadResidualChart(" in js.text
    assert "function forecastHourGridFlowsKwh(" in js.text
    assert 'id="kpiExportAvgCards"' in body
    assert "function renderKpiExportAvg(" in js.text
    assert "/api/kpi/export-avg" in js.text
    # Bilans+SOC must use authoritative net_kwh for imp/exp (same as table), not
    # reconstruct from PV/load/EV + policy_battery_delta_kwh.
    assert "r.net_kwh" in js.text
    assert "policy_battery_delta_kwh" not in js.text.split("function forecastHourGridFlowsKwh(")[1].split(
        "function "
    )[0]
    assert 'id="evDeliveredKwh"' in body
    assert 'id="evRemainingKwh"' in body
    assert 'id="evChargingTotalHint"' in body
    assert 'id="evTargetKwh"' not in body
    assert 'id="evMaxPowerKw" min="1" max="22" step="0.5" value="3"' in body
    assert "EV_DEFAULT_POWER_KW = 3" in js.text
    assert "remaining_kwh: remaining" in js.text
    assert 'getElementById("evTargetKwh")' not in js.text
    assert 'getElementById("evRemainingKwh")' in js.text
