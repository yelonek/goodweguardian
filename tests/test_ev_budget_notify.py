"""Komunikat o 8:00 raz i przekroczenie tylko gdy ładowarka idzie."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ev_day_budget import DayChargingBudget
from ev_budget_notify import process_ev_budget_notices

TZ = ZoneInfo("Europe/Warsaw")


def _budget(kwh: float) -> DayChargingBudget:
    return DayChargingBudget(
        surplus_kwh=kwh / 0.8,
        budget_kwh=kwh,
        coefficient=0.8,
        window_start_hour=10,
        window_end_hour=15,
    )


def test_morning_once_and_over_budget_only_while_charging(tmp_path) -> None:
    path = tmp_path / "notice.json"
    sent: list[tuple[str, str]] = []

    def send(title: str, body: str, **_kwargs: object) -> bool:
        sent.append((title, body))
        return True

    before = datetime(2026, 9, 28, 7, 30, tzinfo=TZ)
    assert process_ev_budget_notices(
        now=before,
        delivered_since_8_kwh=0.0,
        budget=_budget(8.0),
        charging_kw=None,
        path=path,
        send=send,
    ) == []

    at_eight = datetime(2026, 9, 28, 8, 1, tzinfo=TZ)
    assert process_ev_budget_notices(
        now=at_eight,
        delivered_since_8_kwh=0.0,
        budget=_budget(8.0),
        charging_kw=None,
        path=path,
        send=send,
    ) == ["morning"]
    assert process_ev_budget_notices(
        now=at_eight.replace(minute=5),
        delivered_since_8_kwh=0.0,
        budget=_budget(8.0),
        charging_kw=None,
        path=path,
        send=send,
    ) == []

    quiet = process_ev_budget_notices(
        now=at_eight.replace(hour=11),
        delivered_since_8_kwh=9.0,
        budget=_budget(8.0),
        charging_kw=0.0,
        path=path,
        send=send,
    )
    assert quiet == []

    first = process_ev_budget_notices(
        now=at_eight.replace(hour=11, minute=2),
        delivered_since_8_kwh=9.0,
        budget=_budget(8.0),
        charging_kw=1.4,
        path=path,
        send=send,
    )
    assert first == ["over_budget"]
    again = process_ev_budget_notices(
        now=at_eight.replace(hour=11, minute=3),
        delivered_since_8_kwh=9.2,
        budget=_budget(8.0),
        charging_kw=1.4,
        path=path,
        send=send,
    )
    assert again == []

    dropped = process_ev_budget_notices(
        now=at_eight.replace(hour=14),
        delivered_since_8_kwh=9.2,
        budget=_budget(6.0),
        charging_kw=1.4,
        path=path,
        send=send,
    )
    assert dropped == ["over_budget"]
    assert len(sent) == 3
