"""Testy optymalizatora wieloscenariuszowego (shared EV 5×5)."""

from __future__ import annotations

import pytest

from planner.battery import BatteryParams
from planner.models import HourInputs
from planner.optimizer import optimize_horizon
from planner.policy_output import map_hour_to_exec_mode
from planner.scenario_optimizer import optimize_horizon_scenarios
from planner.scenarios import QUANTILES, scenario_name


def _evening_export_morning_risk_hours() -> list[HourInputs]:
    """Wieczorny szczyt RCE + drogi poranek bez PV w niskich kwantylach."""
    return [
        HourInputs(
            date="2026-06-14",
            hour=21,
            load_kwh=0.5,
            pv_kwh=0.02,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.05,
            load_kwh_p25=0.4,
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
            load_kwh_p25=0.4,
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
            load_kwh_p25=0.35,
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
            load_kwh_p25=0.7,
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
            load_kwh_p25=0.4,
            load_kwh_p75=0.55,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.78,
        ),
    ]


def test_scenario_exports_at_high_rce() -> None:
    """RCE > import: zrzut zostaje."""
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-06-18",
            hour=18,
            load_kwh=0.8,
            pv_kwh=0.1,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.2,
            load_kwh_p25=0.6,
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
            load_kwh_p25=0.4,
            load_kwh_p75=0.7,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.56,
        ),
    ]
    res = optimize_horizon_scenarios(hours, soc_start_pct=50.0, params=bp)
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("fallback") != "deterministic_p50"
    assert res.scenario_meta.get("model") == "shared_battery_grid_recourse"
    assert res.hours[0].target_net_kwh > 0.5


def test_optimize_horizon_uses_shared_when_scenarios_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import planner.config as cfg

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "1")
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = _evening_export_morning_risk_hours()
    res = optimize_horizon(hours, soc_start_pct=61.0, params=bp)
    assert res.hours
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("model") == "shared_battery_grid_recourse"


def test_shared_plan_differs_from_det_p50_when_p10_would_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tani zrzut + p10 z drogim importem: plan = shared, nie overlay det p50.

    Nie asertujemy zakazu zrzutu — tylko że trajektoria nie jest kopiowana z p50.
    """
    import planner.config as cfg

    bp = BatteryParams(
        capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0
    )
    hours = [
        HourInputs(
            date="2026-09-15",
            hour=6,
            load_kwh=0.3,
            pv_kwh=0.0,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.0,
            load_kwh_p25=0.25,
            load_kwh_p75=0.4,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.40,
        ),
        *[
            HourInputs(
                date="2026-09-15",
                hour=h,
                load_kwh=1.5,
                pv_kwh=2.0,
                pv_kwh_p10=0.0,
                pv_kwh_p90=3.0,
                load_kwh_p25=1.2,
                load_kwh_p75=1.8,
                import_pln_per_kwh=1.11,
                export_pln_per_kwh=0.20,
            )
            for h in range(7, 13)
        ],
    ]

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "off")
    p50 = optimize_horizon(hours, soc_start_pct=40.0, params=bp)

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "1")
    shared = optimize_horizon(hours, soc_start_pct=40.0, params=bp)

    assert shared.scenario_meta is not None
    assert shared.scenario_meta.get("model") == "shared_battery_grid_recourse"
    assert shared.scenario_meta.get("fallback") != "deterministic_p50"
    assert len(shared.soc_trajectory_pct) == len(p50.soc_trajectory_pct)
    diverged = any(
        abs(a - b) > 2.0
        for a, b in zip(shared.soc_trajectory_pct, p50.soc_trajectory_pct, strict=True)
    )
    assert diverged, (
        f"shared SOC {shared.soc_trajectory_pct} nie może być kopią det p50 "
        f"{p50.soc_trajectory_pct}"
    )


def test_midday_pv_soak_raises_soc_not_export_then_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tanie PV + później drogi import/EV: soak do baterii, nie eksport-potem-sieć."""
    import planner.config as cfg

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "1")

    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.2)
    hours = [
        HourInputs(
            date="2026-07-17",
            hour=10,
            load_kwh=1.0,
            pv_kwh=4.4,
            pv_kwh_p10=1.5,
            pv_kwh_p90=5.0,
            load_kwh_p25=0.8,
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
            load_kwh_p25=0.9,
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
            load_kwh_p25=0.8,
            load_kwh_p75=1.2,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.510,
        ),
        HourInputs(
            date="2026-07-17",
            hour=13,
            load_kwh=12.8,
            pv_kwh=4.6,
            pv_kwh_p10=2.0,
            pv_kwh_p90=5.2,
            load_kwh_p25=12.0,
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
            load_kwh_p25=0.4,
            load_kwh_p75=0.6,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.08,
        ),
    ]
    res = optimize_horizon_scenarios(hours, soc_start_pct=10.0, params=bp)
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("model") == "shared_battery_grid_recourse"

    by_h = {hp.hour: hp for hp in res.hours}
    midday_charge = (
        by_h[10].battery_delta_kwh + by_h[11].battery_delta_kwh + by_h[12].battery_delta_kwh
    )
    assert midday_charge > 0.8
    assert by_h[12].soc_end_pct > by_h[10].soc_start_pct + 8.0

    row12 = map_hour_to_exec_mode(
        by_h[12],
        hours[2],
        cheap_import_threshold_pln=0.61,
    )
    assert row12.exec_mode == "neutral"
    assert row12.exec_mode != "export_pv_surplus"


def test_cheap_pv_then_expensive_import_soaks_not_export_then_grid() -> None:
    """Tanie PV w południe, wieczorem drogi import bez PV: soak, nie eksport-potem-sieć."""
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-07-17",
            hour=h,
            load_kwh=1.0,
            pv_kwh=pv,
            pv_kwh_p10=pv * 0.7,
            pv_kwh_p90=pv * 1.1,
            load_kwh_p25=0.8,
            load_kwh_p75=1.2,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.22,
        )
        for h, pv in ((10, 4.4), (11, 4.9), (12, 5.0))
    ] + [
        HourInputs(
            date="2026-07-17",
            hour=h,
            load_kwh=1.8,
            pv_kwh=0.0,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.0,
            load_kwh_p25=1.5,
            load_kwh_p75=2.1,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.20,
        )
        for h in (18, 19, 20)
    ]
    res = optimize_horizon_scenarios(hours, soc_start_pct=15.0, params=bp)
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("fallback") != "deterministic_p50"
    by_h = {hp.hour: hp for hp in res.hours}
    assert by_h[12].soc_end_pct >= 45.0
    midday_export = sum(max(0.0, by_h[h].target_net_kwh) for h in (10, 11, 12))
    midday_charge = sum(max(0.0, by_h[h].battery_delta_kwh) for h in (10, 11, 12))
    assert midday_charge > midday_export
    evening_discharge = sum(-min(0.0, by_h[h].battery_delta_kwh) for h in (18, 19, 20))
    assert evening_discharge > 1.5


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
            load_kwh_p25=1.8,
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
            load_kwh_p25=1.6,
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
            load_kwh_p25=0.4,
            load_kwh_p75=0.6,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.78,
        ),
    ]
    res = optimize_horizon_scenarios(hours, soc_start_pct=58.0, params=bp)
    h14 = res.hours[0]
    if h14.battery_delta_kwh > 0.05:
        assert h14.target_net_kwh >= -0.05


def test_shared_5x5_converges_with_25_series() -> None:
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = _evening_export_morning_risk_hours()
    res = optimize_horizon_scenarios(hours, soc_start_pct=61.0, params=bp)
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("model") == "shared_battery_grid_recourse"
    assert res.scenario_meta.get("fallback") != "deterministic_p50"
    detail = res.scenarios_detail
    assert detail is not None
    assert detail.model == "shared_battery_grid_recourse"
    expected_keys = {scenario_name(qpv, qld) for qpv in QUANTILES for qld in QUANTILES}
    assert set(detail.scenarios.keys()) == expected_keys
    assert abs(sum(s.weight for s in detail.scenarios.values()) - 1.0) < 1e-9
    assert len(detail.soc_star_pct) == len(hours) + 1
    assert detail.soc_star_pct == pytest.approx(res.soc_trajectory_pct)
    socs = [s.soc_pct for s in detail.scenarios.values()]
    assert all(s == socs[0] for s in socs)
    for name, series in detail.scenarios.items():
        assert len(series.soc_pct) == len(hours) + 1, name
        assert len(series.net_kwh) == len(hours), name
        assert len(series.cashflow_hour_pln) == len(hours), name
        assert series.weight == pytest.approx(1.0 / 25.0)
        assert abs(sum(series.cashflow_hour_pln) - series.cashflow_pln) < 1e-5


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
        optimizer="lp_battery_scenarios_v1",
        inputs_snapshot={},
        hours=res.hours,
        scenarios_detail=res.scenarios_detail,
    )
    raw = plan.model_dump()
    assert raw["scenarios_detail"] is not None
    assert "pv50_ld50" in raw["scenarios_detail"]["scenarios"]
    assert len(raw["scenarios_detail"]["scenarios"]) == 25
    assert len(raw["scenarios_detail"]["scenarios"]["pv50_ld50"]["soc_pct"]) == len(hours) + 1
    roundtrip = DailyPlan.model_validate(raw)
    assert roundtrip.scenarios_detail is not None
    assert isinstance(roundtrip.scenarios_detail, ScenariosDetail)
    assert isinstance(
        roundtrip.scenarios_detail.scenarios["pv50_ld50"], ScenarioSeriesDetail
    )
