"""Budżet ładowania: PV − dom od 8:00, bez target_net, przełącznik do Planera."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from ev_charging_plan import EvChargingDeclaration
from ev_day_budget import (
    compute_day_charging_budget,
    delivered_kwh_since_budget_hour,
    ev_schedule_for_planner,
)


def _row(hour: int, *, pv: float, load: float, rce: float, export: float = 0.0) -> dict:
    return {
        "date": "2026-09-28",
        "hour": hour,
        "pv_kwh": pv,
        "load_base_kwh": load,
        "rce_pln_kwh": rce,
        "export_kwh": export,
        "score": float(hour),
    }


def test_budget_skips_before_8_and_rce_at_or_above_threshold() -> None:
    hours = [
        _row(7, pv=4.0, load=1.0, rce=0.20),
        _row(9, pv=4.0, load=1.0, rce=0.59),
        _row(10, pv=4.0, load=1.5, rce=0.40, export=99.0),
        _row(11, pv=4.0, load=1.5, rce=0.30),
        _row(16, pv=5.0, load=1.0, rce=0.80),
    ]
    budget = compute_day_charging_budget(hours, coefficient=0.80)
    assert budget.surplus_kwh == pytest.approx(5.0)
    assert budget.budget_kwh == pytest.approx(4.0)
    assert budget.window_start_hour == 10
    assert budget.window_end_hour == 11


def test_switch_off_is_empty_even_with_surplus() -> None:
    now = datetime(2026, 9, 28, 8, 0)
    schedule, dump = ev_schedule_for_planner(
        now,
        enabled=False,
        coefficient=0.80,
        slot_rows=[_row(10, pv=4.0, load=1.0, rce=0.20)],
    )
    assert schedule == {}
    assert dump == {}


def test_switch_on_slot_sum_equals_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ev_charging_plan.twc_enabled", lambda: False)
    now = datetime(2026, 9, 28, 8, 0)
    rows = [
        _row(10, pv=4.0, load=1.5, rce=0.40),
        _row(11, pv=4.0, load=1.5, rce=0.30),
        _row(12, pv=4.0, load=1.5, rce=0.20),
    ]
    schedule, dump = ev_schedule_for_planner(
        now, enabled=True, coefficient=0.80, slot_rows=rows
    )
    assert dump["budget"]["budget_kwh"] == pytest.approx(6.0)
    assert sum(schedule.values()) == pytest.approx(6.0)


def test_night_kwh_do_not_shrink_budget_daytime_does(monkeypatch: pytest.MonkeyPatch) -> None:
    by_hour = {2: 5.0, 10: 2.0}

    def _hours(_day: object) -> dict[int, float]:
        return by_hour

    monkeypatch.setattr("ev_charging_plan.twc_enabled", lambda: True)
    monkeypatch.setattr("ev_charging_plan.hourly_ev_kwh_from_telemetry", _hours)
    monkeypatch.setattr("tesla_wall_charger.twc_enabled", lambda: True)
    monkeypatch.setattr("tesla_wall_charger.hourly_ev_kwh_from_telemetry", _hours)
    rows = [
        _row(7, pv=4.0, load=1.0, rce=0.20),
        _row(10, pv=4.0, load=1.5, rce=0.40),
        _row(11, pv=4.0, load=1.5, rce=0.30),
        _row(12, pv=4.0, load=1.5, rce=0.20),
        _row(13, pv=4.0, load=1.5, rce=0.20),
        _row(14, pv=4.0, load=1.5, rce=0.20),
    ]
    at_noon = datetime(2026, 9, 28, 12, 0)
    schedule, dump = ev_schedule_for_planner(
        at_noon, enabled=True, coefficient=0.80, slot_rows=rows
    )
    assert dump["budget"]["budget_kwh"] == pytest.approx(10.0)
    assert 7 not in {hour for _, hour in schedule}
    assert sum(schedule.values()) == pytest.approx(8.0)


def test_planner_skips_evening_and_night_even_when_budget_is_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ev_charging_plan.twc_enabled", lambda: False)
    monkeypatch.setattr("ev_day_budget.read_declaration", lambda: None)
    now = datetime(2026, 9, 28, 8, 0)
    rows = [
        _row(12, pv=5.0, load=1.0, rce=0.20),
        _row(17, pv=0.2, load=0.4, rce=0.90),
        _row(20, pv=0.0, load=0.4, rce=0.84),
        _row(22, pv=0.0, load=0.5, rce=0.40),
        _row(23, pv=0.0, load=0.5, rce=0.30),
    ]
    schedule, dump = ev_schedule_for_planner(
        now, enabled=True, coefficient=0.80, slot_rows=rows
    )
    assert set(hour for _, hour in schedule) == {12}
    assert 22 not in {hour for _, hour in schedule}
    assert dump["source"] == "budget"


def test_planner_fills_lowest_rce_ahead_of_a_better_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ev_charging_plan.twc_enabled", lambda: False)
    monkeypatch.setattr("ev_day_budget.read_declaration", lambda: None)
    now = datetime(2026, 9, 28, 8, 0)
    expensive = _row(15, pv=4.0, load=1.0, rce=0.41)
    expensive["score"] = 0.01
    cheap = _row(11, pv=5.0, load=1.0, rce=0.25)
    cheap["score"] = 9.0
    cheapest = _row(12, pv=5.0, load=1.0, rce=0.13)
    cheapest["score"] = 8.0
    schedule, _dump = ev_schedule_for_planner(
        now,
        enabled=True,
        coefficient=3.0 / 11.0,
        slot_rows=[expensive, cheap, cheapest],
    )
    assert set(hour for _, hour in schedule) == {12}
    assert schedule[("2026-09-28", 12)] == pytest.approx(3.0)


def test_manual_target_replaces_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ev_charging_plan.twc_enabled", lambda: False)
    monkeypatch.setattr(
        "ev_day_budget.read_declaration",
        lambda: EvChargingDeclaration(
            date="2026-09-28", target_kwh=1.5, max_power_kw=3.0
        ),
    )
    now = datetime(2026, 9, 28, 8, 0)
    rows = [
        _row(10, pv=4.0, load=1.0, rce=0.20),
        _row(11, pv=4.0, load=1.0, rce=0.30),
        _row(18, pv=0.0, load=0.4, rce=1.00),
    ]
    schedule, dump = ev_schedule_for_planner(
        now, enabled=True, coefficient=0.80, slot_rows=rows
    )
    assert dump["source"] == "manual"
    assert sum(schedule.values()) == pytest.approx(1.5)
    assert set(hour for _, hour in schedule) <= {10, 11}


def test_delivered_since_8_skips_night(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tesla_wall_charger.twc_enabled", lambda: True)
    monkeypatch.setattr(
        "tesla_wall_charger.hourly_ev_kwh_from_telemetry",
        lambda _day: {2: 5.0, 8: 1.0, 11: 0.5},
    )
    assert delivered_kwh_since_budget_hour(date(2026, 9, 28)) == pytest.approx(1.5)
