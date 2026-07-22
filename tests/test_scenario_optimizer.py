"""Testy optymalizatora wieloscenariuszowego (tracking-SP + legacy shared)."""

from __future__ import annotations

import pytest

from planner.battery import BatteryParams
from planner.models import HourInputs
from planner.optimizer import optimize_horizon
from planner.policy_output import map_hour_to_exec_mode
from planner.scenario_optimizer import optimize_horizon_scenarios


def _evening_export_morning_risk_hours() -> list[HourInputs]:
    """Wieczorny szczyt RCE + drogi poranek bez PV w pesymistycznym scenariuszu."""
    return [
        HourInputs(
            date="2026-06-14",
            hour=21,
            load_kwh=0.5,
            pv_kwh=0.02,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.05,
            load_kwh_p75=0.55,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.59,
        ),
        HourInputs(
            date="2026-06-14",
            hour=22,
            load_kwh=0.5,
            pv_kwh=0.0,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.0,
            load_kwh_p75=0.6,
            import_pln_per_kwh=0.59,
            export_pln_per_kwh=0.56,
        ),
        HourInputs(
            date="2026-06-15",
            hour=6,
            load_kwh=0.46,
            pv_kwh=0.3,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.5,
            load_kwh_p75=0.7,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.56,
        ),
        HourInputs(
            date="2026-06-15",
            hour=10,
            load_kwh=1.0,
            pv_kwh=1.5,
            pv_kwh_p10=0.2,
            pv_kwh_p90=2.0,
            load_kwh_p75=2.5,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.14,
        ),
        HourInputs(
            date="2026-06-15",
            hour=20,
            load_kwh=0.5,
            pv_kwh=0.2,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.4,
            load_kwh_p75=0.55,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.78,
        ),
    ]


def test_scenario_exports_at_high_rce() -> None:
    """Regresja: przy wysokim RCE planer musi eksportować, nie neutral."""
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-06-18",
            hour=18,
            load_kwh=0.8,
            pv_kwh=0.1,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.2,
            load_kwh_p75=1.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.69,
        ),
        HourInputs(
            date="2026-06-19",
            hour=6,
            load_kwh=0.5,
            pv_kwh=0.1,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.3,
            load_kwh_p75=0.7,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.56,
        ),
    ]
    res = optimize_horizon_scenarios(hours, soc_start_pct=50.0, params=bp)
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("fallback") != "deterministic_p50"
    assert res.hours[0].target_net_kwh > 0.5


def test_optimize_horizon_uses_tracking_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import planner.config as cfg

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "1")
    monkeypatch.setattr(cfg, "_SOC_TRACKING_RAW", "1")
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = _evening_export_morning_risk_hours()
    res = optimize_horizon(hours, soc_start_pct=61.0, params=bp)
    assert res.hours
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("model") == "soc_tracking_recourse"


def test_tracking_keeps_dawn_reserve_vs_p50(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracking z silną wagą p10 trzyma wyższą rezerwę po nocy niż czysty p50."""
    import planner.config as cfg
    import planner.scenario_optimizer as so
    import planner.scenarios as scen

    bp = BatteryParams(
        capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0
    )
    hours = _evening_export_morning_risk_hours()

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "off")
    p50 = optimize_horizon(hours, soc_start_pct=80.0, params=bp)

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "1")
    monkeypatch.setattr(cfg, "_SOC_TRACKING_RAW", "1")
    monkeypatch.setattr(cfg, "PLANNER_SOC_TRACKING_LAMBDA", 0.25)
    monkeypatch.setattr(so, "PLANNER_SOC_TRACKING_LAMBDA", 0.25)
    monkeypatch.setattr(scen, "PLANNER_SCENARIO_WEIGHT_PESSIMISTIC", 0.6)
    monkeypatch.setattr(scen, "PLANNER_SCENARIO_WEIGHT_BASE", 0.39)
    monkeypatch.setattr(scen, "PLANNER_SCENARIO_WEIGHT_OPTIMISTIC", 0.01)
    tracked = optimize_horizon(hours, soc_start_pct=80.0, params=bp)

    assert tracked.scenario_meta is not None
    assert tracked.scenario_meta.get("model") == "soc_tracking_recourse"
    # Sloty: h21, h22, h6, h10, h20 → traj[2] = SOC po nocy (koniec h22).
    assert len(tracked.soc_trajectory_pct) >= 3
    assert tracked.soc_trajectory_pct[2] > p50.soc_trajectory_pct[2] + 5.0


def test_midday_pv_soak_raises_soc_star_not_export_then_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 17.07: tanie RCE 10–12 + EV@13 → soc* rośnie w południe, bez charge_grid na baterię o 13."""
    import planner.config as cfg
    import planner.scenario_optimizer as so

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "1")
    monkeypatch.setattr(cfg, "_SOC_TRACKING_RAW", "1")
    monkeypatch.setattr(cfg, "PLANNER_SOC_TRACKING_LAMBDA", 0.12)
    monkeypatch.setattr(so, "PLANNER_SOC_TRACKING_LAMBDA", 0.12)

    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.2)
    hours = [
        HourInputs(
            date="2026-07-17",
            hour=10,
            load_kwh=1.0,
            pv_kwh=4.4,
            pv_kwh_p10=1.5,
            pv_kwh_p90=5.0,
            load_kwh_p75=1.2,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.565,
        ),
        HourInputs(
            date="2026-07-17",
            hour=11,
            load_kwh=1.1,
            pv_kwh=4.9,
            pv_kwh_p10=1.6,
            pv_kwh_p90=5.5,
            load_kwh_p75=1.3,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.541,
        ),
        HourInputs(
            date="2026-07-17",
            hour=12,
            load_kwh=1.0,
            pv_kwh=5.0,
            pv_kwh_p10=1.7,
            pv_kwh_p90=5.6,
            load_kwh_p75=1.2,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.510,
        ),
        HourInputs(
            date="2026-07-17",
            hour=13,
            load_kwh=12.8,  # EV 11 + dom
            pv_kwh=4.6,
            pv_kwh_p10=2.0,
            pv_kwh_p90=5.2,
            load_kwh_p75=13.5,
            import_pln_per_kwh=0.59,
            export_pln_per_kwh=0.495,
        ),
        HourInputs(
            date="2026-07-17",
            hour=19,
            load_kwh=0.5,
            pv_kwh=0.3,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.5,
            load_kwh_p75=0.6,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.08,
        ),
    ]
    res = optimize_horizon_scenarios(hours, soc_start_pct=10.0, params=bp)
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("model") == "soc_tracking_recourse"

    by_h = {hp.hour: hp for hp in res.hours}
    # SOC* rośnie przez południe (soak 11–12).
    assert by_h[12].soc_end_pct > by_h[10].soc_start_pct + 15.0
    assert by_h[12].soc_end_pct >= 40.0

    # Godzina z rosnącym SOC → soak (neutral), nie export_pv_surplus.
    row12 = map_hour_to_exec_mode(
        by_h[12],
        hours[2],
        cheap_import_threshold_pln=0.61,
    )
    assert row12.exec_mode == "neutral"
    assert row12.exec_mode != "export_pv_surplus"

    # O 13 bateria nie dobija się z sieci (EV i tak ciągnie import domu).
    assert by_h[13].battery_delta_kwh < 1.0
    assert by_h[13].soc_end_pct >= by_h[12].soc_end_pct - 1.0


def test_scenario_milp_no_grid_charge_when_pv_surplus() -> None:
    """Regresja: baza ładuje z PV, bez importu przy nadwyżce PV."""
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-06-19",
            hour=14,
            load_kwh=2.2,
            pv_kwh=4.5,
            pv_kwh_p10=2.0,
            pv_kwh_p90=5.5,
            load_kwh_p75=8.5,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.59,
        ),
        HourInputs(
            date="2026-06-19",
            hour=15,
            load_kwh=2.0,
            pv_kwh=3.8,
            pv_kwh_p10=1.5,
            pv_kwh_p90=4.5,
            load_kwh_p75=7.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.56,
        ),
        HourInputs(
            date="2026-06-19",
            hour=20,
            load_kwh=0.5,
            pv_kwh=0.1,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.2,
            load_kwh_p75=0.6,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.78,
        ),
    ]
    res = optimize_horizon_scenarios(hours, soc_start_pct=58.0, params=bp)
    h14 = res.hours[0]
    if h14.battery_delta_kwh > 0.05:
        assert h14.target_net_kwh >= -0.05


def test_legacy_shared_battery_still_available(monkeypatch: pytest.MonkeyPatch) -> None:
    import planner.config as cfg

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "1")
    monkeypatch.setattr(cfg, "_SOC_TRACKING_RAW", "off")
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    res = optimize_horizon_scenarios(
        _evening_export_morning_risk_hours(), soc_start_pct=61.0, params=bp
    )
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("model") == "shared_battery_grid_recourse"
    assert res.scenarios_detail is not None
    assert res.scenarios_detail.model == "shared_battery_grid_recourse"
    # Shared SOC: wszystkie scenariusze mają tę samą trajektorię.
    socs = [s.soc_pct for s in res.scenarios_detail.scenarios.values()]
    assert len(socs) == 3
    assert socs[0] == socs[1] == socs[2]


def test_tracking_scenarios_detail_has_three_soc_trajectories() -> None:
    """Tracking-SP: scenarios_detail z 3 trajektoriami SOC (H+1) + net/CF_h (H)."""
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = _evening_export_morning_risk_hours()
    res = optimize_horizon_scenarios(hours, soc_start_pct=61.0, params=bp)
    detail = res.scenarios_detail
    assert detail is not None
    assert detail.model == "soc_tracking_recourse"
    assert len(detail.soc_star_pct) == len(hours) + 1
    assert detail.soc_star_pct == pytest.approx(res.soc_trajectory_pct)
    assert set(detail.scenarios.keys()) == {"pessimistic", "base", "optimistic"}
    assert len(detail.slots) == len(hours)
    for name, series in detail.scenarios.items():
        assert len(series.soc_pct) == len(hours) + 1, name
        assert len(series.net_kwh) == len(hours), name
        assert len(series.cashflow_hour_pln) == len(hours), name
        assert series.weight > 0
        assert abs(sum(series.cashflow_hour_pln) - series.cashflow_pln) < 1e-6


def test_scenarios_detail_serializes_into_daily_plan() -> None:
    """DailyPlan.model_dump zachowuje scenarios_detail (plan_latest)."""
    from datetime import UTC, datetime

    from planner.models import DailyPlan, ScenariosDetail, ScenarioSeriesDetail

    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = _evening_export_morning_risk_hours()
    res = optimize_horizon_scenarios(hours, soc_start_pct=50.0, params=bp)
    assert res.scenarios_detail is not None

    plan = DailyPlan(
        plan_id="sc-viz-test",
        local_date="2026-06-14",
        generated_at=datetime.now(UTC).isoformat(),
        timezone="Europe/Warsaw",
        horizon_start="2026-06-14T21:00:00",
        horizon_end="2026-06-15T20:00:00",
        soc_start_pct=50.0,
        soc_trajectory_pct=list(res.soc_trajectory_pct),
        expected_total_cashflow_pln=res.total_cashflow_pln,
        optimizer="lp_soc_tracking_v1",
        inputs_snapshot={},
        hours=res.hours,
        scenarios_detail=res.scenarios_detail,
    )
    raw = plan.model_dump()
    assert raw["scenarios_detail"] is not None
    assert "pessimistic" in raw["scenarios_detail"]["scenarios"]
    assert len(raw["scenarios_detail"]["scenarios"]["base"]["soc_pct"]) == len(hours) + 1
    roundtrip = DailyPlan.model_validate(raw)
    assert roundtrip.scenarios_detail is not None
    assert isinstance(roundtrip.scenarios_detail, ScenariosDetail)
    assert isinstance(
        roundtrip.scenarios_detail.scenarios["base"], ScenarioSeriesDetail
    )
