"""Horyzont planowania — wykrywanie RCE, bez sztywnego harmonogramu."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from planner.pricing_horizon import priced_horizon_slots


def test_priced_horizon_today_only(monkeypatch: pytest.MonkeyPatch) -> None:
    today = date(2026, 6, 7)
    now = datetime(2026, 6, 7, 10, 30, 0)

    def fake_pricing(d: date) -> bool:
        return d == today

    monkeypatch.setattr(
        "planner.pricing_horizon.pricing_available_for_day", fake_pricing
    )
    slots = priced_horizon_slots(now=now)
    assert slots == [(today.isoformat(), h) for h in range(10, 24)]


def test_priced_horizon_includes_tomorrow_when_rce_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today = date(2026, 6, 7)
    tomorrow = date(2026, 6, 8)
    now = datetime(2026, 6, 7, 14, 0, 0)

    def fake_pricing(d: date) -> bool:
        return d in (today, tomorrow)

    monkeypatch.setattr(
        "planner.pricing_horizon.pricing_available_for_day", fake_pricing
    )
    slots = priced_horizon_slots(now=now)
    assert slots[0] == (today.isoformat(), 14)
    assert slots[-1] == (tomorrow.isoformat(), 23)
    assert len(slots) == (24 - 14) + 24
