"""Testy ev_charging_plan — alokacja i rekomendacja."""

from __future__ import annotations

from datetime import datetime

import pytest

from ev_charging_plan import (
    EvChargingDeclaration,
    allocate_ev_schedule,
    compute_cheap_budget,
    ev_schedule_map,
)


def _slot_row(
    d: str,
    h: int,
    *,
    import_pln: float = 0.5,
    rce: float = 0.3,
    pv: float = 2.0,
    is_night: bool = False,
    is_cheap: bool = True,
    score: float = 0.4,
) -> dict:
    return {
        "date": d,
        "hour": h,
        "import_pln_per_kwh": import_pln,
        "rce_pln_kwh": rce,
        "pv_kwh": pv,
        "is_g12_night": is_night,
        "is_cheap": is_cheap,
        "score": score,
    }


def test_greedy_picks_cheapest_hours_first() -> None:
    d = "2026-06-11"
    rows = [
        _slot_row(d, 8, score=0.2, rce=0.1, pv=3.0),
        _slot_row(d, 9, score=0.25, rce=0.15, pv=2.5),
        _slot_row(d, 14, score=0.9, rce=0.8, pv=0.2, is_cheap=False),
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
        _slot_row(d, 8, score=0.15, rce=0.1, pv=4.0),
        _slot_row(d, 9, score=0.2, rce=0.2, pv=3.0),
        _slot_row(d, 10, score=0.5, rce=0.4, pv=1.0),
    ]
    decl = EvChargingDeclaration(
        date=d, target_kwh=11.0, preferred_start_hour=10, max_power_kw=11.0
    )
    now = datetime(2026, 6, 11, 7, 0)
    plan = allocate_ev_schedule(decl, rows, now=now)
    assert plan.slots[0].hour == 10
    assert any("rozważ wcześniejsze" in w.lower() for w in plan.warnings)


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


def test_cheap_budget_sums_pv_and_import() -> None:
    d = "2026-06-11"
    rows = [
        _slot_row(d, 8, pv=3.0, rce=0.2, is_night=False),
        _slot_row(d, 22, pv=0.0, rce=0.5, is_night=True, score=0.3),
    ]
    now = datetime(2026, 6, 11, 7, 0)
    budget = compute_cheap_budget(rows, now=now, max_power_kw=11.0)
    assert budget.cheap_pv_kwh == pytest.approx(3.0)
    assert budget.cheap_import_kwh == pytest.approx(11.0)
    assert budget.recommendable_kwh == pytest.approx(14.0)
