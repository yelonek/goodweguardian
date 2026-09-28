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
    assert "function _slotXTicks(" in js.text
    assert 'yUnit: "SOC %"' in js.text
    assert ".plan-sc-chart .tick-label" in body
    assert 'id="kpiExportAvgCards"' in body
    assert "function renderKpiExportAvg(" in js.text
    assert "/api/kpi/export-avg" in js.text
    # Bilans+SOC must use authoritative net_kwh for imp/exp (same as table), not
    # reconstruct from PV/load/EV + policy_battery_delta_kwh.
    assert "r.net_kwh" in js.text
    assert "policy_battery_delta_kwh" not in js.text.split("function forecastHourGridFlowsKwh(")[1].split(
        "function "
    )[0]
    assert 'id="evBudgetCard"' in body
    assert 'id="evBudgetCoefficient"' in body
    assert 'id="evChargingPlanEnabled"' in body
    assert 'id="applyDayPlan"' in js.text
    assert "Stosuj plan ładowania" in body
    assert 'id="evRemainingKwh"' not in body
    assert 'getElementById("evRemainingKwh")' not in js.text
    assert 'data-page="charging">Ładowanie</a>' in body
    assert 'id="page-charging"' in body
    assert "function loadCharging(" in js.text
    assert "function renderEvBudgetCard(" in js.text
    assert "charging: loadCharging" in js.text
    forecast = body.split('id="page-forecast"', 1)[1].split('id="page-charging"', 1)[0]
    charging = body.split('id="page-charging"', 1)[1].split('id="page-pv-correction"', 1)[0]
    overview = body.split('id="page-overview"', 1)[1].split('id="page-history"', 1)[0]
    assert "evBudgetCard" not in forecast
    assert "pvPyramidBlock" not in overview
    assert 'id="evBudgetCard"' in charging
    assert 'id="pvPyramidBlock"' in charging
    assert 'class="forecast-day-chart-block wide"' in forecast
    assert "viewBox=\"0 0 960 320\"" in forecast
