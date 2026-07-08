"""Testy integracji EV z wejściami planera."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from ev_charging_plan import EvChargingDeclaration, EvChargingPlan, EvChargingSlot
from planner.inputs import build_hour_inputs_for_slots


def test_build_hour_inputs_adds_ev_to_load(monkeypatch: pytest.MonkeyPatch) -> None:
    d = "2026-06-11"
    decl = EvChargingDeclaration(
        date=d, target_kwh=11.0, preferred_start_hour=10, max_power_kw=11.0
    )
    plan = EvChargingPlan(
        date=d,
        declaration=decl,
        slots=[EvChargingSlot(date=d, hour=10, kwh=11.0)],
        warnings=[],
        recommended_slots=[],
    )

    monkeypatch.setattr(
        "planner.inputs.declarations_for_dates",
        lambda dates: {d: decl} if d in dates else {},
    )
    monkeypatch.setattr(
        "planner.inputs.build_ev_charging_plan",
        lambda declaration=None, **kwargs: plan,
    )
    monkeypatch.setattr(
        "planner.inputs.forecast_load_hours",
        lambda **kwargs: {
            "hours": [
                {
                    "date": d,
                    "hour": 10,
                    "load_kwh_p50": 0.5,
                    "load_kwh_p25": 0.4,
                    "load_kwh_p75": 0.6,
                    "load_base_kwh_p50": 0.5,
                    "source": "test",
                }
            ]
        },
    )
    monkeypatch.setattr(
        "planner.inputs.fetch_hourly_pv_forecast",
        lambda **kwargs: {"hours": [{"date": d, "hour": 10, "pv_kw": 2.0}]},
    )
    monkeypatch.setattr(
        "planner.inputs.apply_pv_correction",
        lambda slots, pv_by_key, now=None: ({(d, 10): 2.0}, {(d, 10): "test"}, {}),
    )
    monkeypatch.setattr(
        "planner.inputs.pricing_day_breakdown",
        lambda day: {
            "hours": [
                {
                    "hour": h,
                    "import_pln_per_kwh": 0.59,
                    "rce_pln_kwh": 0.2,
                }
                for h in range(24)
            ]
        },
    )

    inputs, snapshot = build_hour_inputs_for_slots(
        [(d, 10)], now=datetime(2026, 6, 11, 8, 0)
    )
    assert len(inputs) == 1
    hin = inputs[0]
    assert hin.load_base_kwh == pytest.approx(0.5)
    assert hin.ev_load_kwh == pytest.approx(11.0)
    assert hin.load_kwh == pytest.approx(11.5)
    assert hin.load_kwh_p75 == pytest.approx(11.5)
    assert "ev_charging_plans" in snapshot
