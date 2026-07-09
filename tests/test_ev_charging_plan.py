"""Testy ev_charging_plan — alokacja i rekomendacja."""

from __future__ import annotations

from datetime import datetime

import pytest

from ev_charging_plan import (
    EvChargingDeclaration,
    allocate_ev_schedule,
    build_horizon_slot_rows,
    compute_cheap_budget,
    export_kwh_for_slot,
    ev_schedule_map,
)
from planner.models import DailyPlan, HourPlan


def _slot_row(
    d: str,
    h: int,
    *,
    import_pln: float = 0.5,
    rce: float = 0.3,
    pv: float = 2.0,
    export_kwh: float | None = None,
    load_base_kwh: float = 0.0,
    is_night: bool = False,
    is_cheap: bool = True,
    score: float = 0.4,
) -> dict:
    exp = export_kwh if export_kwh is not None else max(0.0, pv - load_base_kwh)
    return {
        "date": d,
        "hour": h,
        "import_pln_per_kwh": import_pln,
        "rce_pln_kwh": rce,
        "pv_kwh": pv,
        "load_base_kwh": load_base_kwh,
        "export_kwh": exp,
        "is_g12_night": is_night,
        "is_cheap": is_cheap,
        "score": score,
    }


def _minimal_plan(d: str, hour: int, target_net_kwh: float) -> DailyPlan:
    return DailyPlan(
        plan_id="test-plan",
        local_date=d,
        generated_at="2026-06-11T07:00:00",
        timezone="Europe/Warsaw",
        soc_start_pct=50.0,
        expected_total_cashflow_pln=0.0,
        optimizer="test",
        inputs_snapshot={},
        hours=[
            HourPlan(
                date=d,
                hour=hour,
                target_net_kwh=target_net_kwh,
                expected_cashflow_pln=0.0,
                soc_start_pct=50.0,
                soc_end_pct=50.0,
                battery_delta_kwh=0.0,
            )
        ],
    )


def test_greedy_picks_cheapest_hours_first() -> None:
    d = "2026-06-11"
    rows = [
        _slot_row(d, 8, score=0.2, rce=0.1, pv=3.0, export_kwh=3.0),
        _slot_row(d, 9, score=0.25, rce=0.15, pv=2.5, export_kwh=2.5),
        _slot_row(d, 14, score=0.9, rce=0.8, pv=0.2, export_kwh=0.0, is_cheap=False),
    ]
    decl = EvChargingDeclaration(date=d, target_kwh=15.0, max_power_kw=11.0)
    now = datetime(2026, 6, 11, 7, 0)
    plan = allocate_ev_schedule(decl, rows, now=now)
    assert len(plan.slots) == 2
    assert plan.slots[0].hour == 8
    assert plan.slots[0].kwh == pytest.approx(11.0)
    assert plan.slots[1].hour == 9
    assert plan.slots[1].kwh == pytest.approx(4.0)


def test_preferred_start_warns_about_cheaper_earlier() -> None:
    d = "2026-06-11"
    rows = [
        _slot_row(d, 8, score=0.15, rce=0.1, pv=4.0, export_kwh=4.0),
        _slot_row(d, 9, score=0.2, rce=0.2, pv=3.0, export_kwh=3.0),
        _slot_row(d, 10, score=0.5, rce=0.4, pv=1.0, export_kwh=1.0),
    ]
    decl = EvChargingDeclaration(
        date=d, target_kwh=11.0, preferred_start_hour=10, max_power_kw=11.0
    )
    now = datetime(2026, 6, 11, 7, 0)
    plan = allocate_ev_schedule(decl, rows, now=now)
    assert plan.slots[0].hour == 10
    assert any("rozważ wcześniejsze" in w.lower() for w in plan.warnings)
    assert any("taniego eksportu" in w.lower() for w in plan.warnings)


def test_manual_slots_validated() -> None:
    d = "2026-06-11"
    decl = EvChargingDeclaration(
        date=d,
        target_kwh=5.0,
        manual_slots={10: 6.0},
        max_power_kw=11.0,
    )
    with pytest.raises(ValueError, match="manual_slots"):
        allocate_ev_schedule(decl, [_slot_row(d, 10)], now=datetime(2026, 6, 11, 7))


def test_manual_slots_used() -> None:
    d = "2026-06-11"
    decl = EvChargingDeclaration(
        date=d,
        target_kwh=10.0,
        manual_slots={10: 7.0, 11: 3.0},
        max_power_kw=11.0,
    )
    plan = allocate_ev_schedule(
        decl, [_slot_row(d, h) for h in range(8, 12)], now=datetime(2026, 6, 11, 7)
    )
    m = ev_schedule_map(plan)
    assert m[(d, 10)] == pytest.approx(7.0)
    assert m[(d, 11)] == pytest.approx(3.0)


def test_cheap_budget_sums_export_and_import() -> None:
    d = "2026-06-11"
    rows = [
        _slot_row(d, 8, pv=5.0, export_kwh=2.0, rce=0.2, is_night=False),
        _slot_row(d, 22, pv=0.0, export_kwh=0.0, rce=0.5, is_night=True, score=0.3),
    ]
    now = datetime(2026, 6, 11, 7, 0)
    budget = compute_cheap_budget(rows, now=now, max_power_kw=11.0)
    assert budget.cheap_export_kwh == pytest.approx(2.0)
    assert budget.cheap_import_kwh == pytest.approx(11.0)
    assert budget.recommendable_kwh == pytest.approx(13.0)


def test_export_kwh_from_plan_when_available() -> None:
    d = "2026-06-11"
    plan = _minimal_plan(d, 8, target_net_kwh=2.0)
    export = export_kwh_for_slot(
        d, 8, pv_kwh=5.0, load_base_kwh=1.0, plan=plan
    )
    assert export == pytest.approx(2.0)


def test_export_kwh_fallback_pv_minus_load() -> None:
    d = "2026-06-11"
    export = export_kwh_for_slot(
        d, 8, pv_kwh=5.0, load_base_kwh=3.0, plan=None
    )
    assert export == pytest.approx(2.0)


def test_build_horizon_slot_rows_uses_plan_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = "2026-06-11"
    plan = _minimal_plan(d, 8, target_net_kwh=1.5)
    monkeypatch.setattr(
        "ev_charging_plan.load_latest_plan",
        lambda: plan,
    )
    monkeypatch.setattr(
        "ev_charging_plan.fetch_hourly_pv_forecast",
        lambda **_: {
            "hours": [{"date": d, "hour": 8, "pv_kw": 4.0}],
        },
    )
    monkeypatch.setattr(
        "ev_charging_plan.forecast_load_hours",
        lambda **_: {
            "hours": [
                {
                    "date": d,
                    "hour": 8,
                    "load_kwh_p50": 1.0,
                    "load_base_kwh_p50": 1.0,
                }
            ],
        },
    )
    monkeypatch.setattr(
        "ev_charging_plan.pricing_day_breakdown",
        lambda _d: {
            "hours": {
                8: {"import_pln_per_kwh": 0.5, "rce_pln_kwh": 0.2},
            }
        },
    )
    rows = build_horizon_slot_rows([(d, 8)])
    assert rows[0]["export_kwh"] == pytest.approx(1.5)
    assert rows[0]["load_base_kwh"] == pytest.approx(1.0)


def test_past_hour_slot_not_shifted_after_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Po zakończeniu h ze startem EV energia nie przesuwa się na kolejną godzinę."""
    d = "2026-07-08"
    rows = [_slot_row(d, h, score=0.5) for h in range(8, 18)]
    decl = EvChargingDeclaration(
        date=d, target_kwh=1.0, preferred_start_hour=12, max_power_kw=4.0
    )
    monkeypatch.setattr("ev_charging_plan.twc_enabled", lambda: True)
    monkeypatch.setattr(
        "ev_charging_plan.hourly_ev_kwh_from_telemetry",
        lambda _day: {12: 1.0},
    )
    plan = allocate_ev_schedule(decl, rows, now=datetime(2026, 7, 8, 13, 5))
    assert plan.delivered_kwh == pytest.approx(1.0)
    assert plan.remaining_kwh == pytest.approx(0.0)
    assert plan.slots == []
    assert len(plan.past_slots) == 1
    assert plan.past_slots[0].hour == 12
    assert ev_schedule_map(plan) == {}


def test_partial_delivery_allocates_only_remainder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = "2026-07-08"
    rows = [_slot_row(d, h, score=0.5) for h in range(8, 18)]
    decl = EvChargingDeclaration(
        date=d, target_kwh=1.0, preferred_start_hour=12, max_power_kw=4.0
    )
    monkeypatch.setattr("ev_charging_plan.twc_enabled", lambda: True)
    monkeypatch.setattr(
        "ev_charging_plan.hourly_ev_kwh_from_telemetry",
        lambda _day: {12: 0.4},
    )
    plan = allocate_ev_schedule(decl, rows, now=datetime(2026, 7, 8, 13, 5))
    assert plan.delivered_kwh == pytest.approx(0.4)
    assert plan.remaining_kwh == pytest.approx(0.6)
    assert len(plan.slots) == 1
    assert plan.slots[0].hour == 13
    assert plan.slots[0].kwh == pytest.approx(0.6)


def test_ev_schedule_map_include_past() -> None:
    d = "2026-07-08"
    from ev_charging_plan import EvChargingPlan, EvChargingSlot

    plan = EvChargingPlan(
        date=d,
        past_slots=[EvChargingSlot(date=d, hour=12, kwh=1.0)],
        slots=[EvChargingSlot(date=d, hour=13, kwh=0.5)],
    )
    assert ev_schedule_map(plan) == {(d, 13): 0.5}
    assert ev_schedule_map(plan, include_past=True) == {
        (d, 12): 1.0,
        (d, 13): 0.5,
    }
