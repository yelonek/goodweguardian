"""Testy wyboru snapshotu PV z /history (prognoza sprzed :00)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pv_forecast import (
    _aggregate_items_to_hours,
    _aggregate_single_hour,
    _pre_hour_forecast_items,
    _to_local_hour,
    fetch_hourly_pv_forecast_with_history,
)

TZ = ZoneInfo("Europe/Warsaw")


def _hist_item(*, fetched_at: str, period_end: str, pv: float) -> dict:
    return {
        "fetched_at": fetched_at,
        "period_end": period_end,
        "pv_estimate": pv,
        "pv_estimate10": pv * 0.8,
        "pv_estimate90": pv * 1.2,
    }


def test_to_local_hour_uses_period_start_not_period_end() -> None:
    """PT30M: period_end 10:00 CEST = okno 09:30–10:00 → godzina 9."""
    # 08:00Z = 10:00 CEST end → start 09:30 → h09
    assert _to_local_hour("2026-07-09T08:00:00.0000000Z") == ("2026-07-09", 9)
    # 08:30Z = 10:30 CEST end → start 10:00 → h10
    assert _to_local_hour("2026-07-09T08:30:00.0000000Z") == ("2026-07-09", 10)
    # 09:00Z = 11:00 CEST end → start 10:30 → h10
    assert _to_local_hour("2026-07-09T09:00:00.0000000Z") == ("2026-07-09", 10)


def test_aggregate_hour_uses_both_half_hours_of_local_clock_hour() -> None:
    """Godzina lokalna 10 = sloty kończące się 10:30 i 11:00 (nie 10:00 i 10:30)."""
    items = [
        {
            "period_end": "2026-07-09T08:00:00.0000000Z",  # → h09
            "pv_estimate": 1.0,
            "pv_estimate10": 0.8,
            "pv_estimate90": 1.2,
        },
        {
            "period_end": "2026-07-09T08:30:00.0000000Z",  # → h10
            "pv_estimate": 4.0,
            "pv_estimate10": 3.2,
            "pv_estimate90": 4.8,
        },
        {
            "period_end": "2026-07-09T09:00:00.0000000Z",  # → h10
            "pv_estimate": 6.0,
            "pv_estimate10": 4.8,
            "pv_estimate90": 7.2,
        },
    ]
    rows = { (r["date"], r["hour"]): r for r in _aggregate_items_to_hours(items) }
    assert rows[("2026-07-09", 9)]["pv_kw"] == pytest.approx(1.0)
    assert rows[("2026-07-09", 10)]["pv_kw"] == pytest.approx(5.0)


def test_pre_hour_picks_latest_fetch_before_slot_start() -> None:
    """Dla h10 bierzemy fetch 09:55, nie starszy 06:55 ani 10:30 po :00."""
    # h10 CEST = period_end 08:30Z + 09:00Z (okna 10:00–10:30 i 10:30–11:00)
    items = [
        _hist_item(
            fetched_at="2026-07-09T06:55:01",
            period_end="2026-07-09T08:30:00.0000000Z",
            pv=1.0,
        ),
        _hist_item(
            fetched_at="2026-07-09T06:55:01",
            period_end="2026-07-09T09:00:00.0000000Z",
            pv=1.2,
        ),
        _hist_item(
            fetched_at="2026-07-09T09:55:01",
            period_end="2026-07-09T08:30:00.0000000Z",
            pv=3.5,
        ),
        _hist_item(
            fetched_at="2026-07-09T09:55:01",
            period_end="2026-07-09T09:00:00.0000000Z",
            pv=3.6,
        ),
        _hist_item(
            fetched_at="2026-07-09T10:30:01",
            period_end="2026-07-09T08:30:00.0000000Z",
            pv=9.9,
        ),
    ]
    picked = _pre_hour_forecast_items(items, date_s="2026-07-09", hour=10)
    assert len(picked) == 2
    assert all(i["fetched_at"].startswith("2026-07-09T09:55") for i in picked)
    row = _aggregate_single_hour(picked, date_s="2026-07-09", hour=10)
    assert row is not None
    assert row["pv_kw"] == pytest.approx(3.55, rel=0.01)


def test_pre_hour_returns_empty_without_pre_slot_fetch() -> None:
    items = [
        _hist_item(
            fetched_at="2026-07-09T10:30:01",
            period_end="2026-07-09T08:30:00.0000000Z",
            pv=2.0,
        ),
    ]
    assert _pre_hour_forecast_items(items, date_s="2026-07-09", hour=10) == []


def test_fetch_with_history_uses_start_end_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"forecasts": captured.get("history", [])}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url: str, params: dict | None = None):
            if url.endswith("/forecasts"):
                return type(
                    "R",
                    (),
                    {
                        "raise_for_status": lambda self: None,
                        "json": lambda self: {
                            "data": {"forecasts": []},
                            "cached": False,
                            "fetched_at": "2026-07-09T10:00:00",
                        },
                    },
                )()
            captured["history_params"] = params
            return FakeResp()

    monkeypatch.setattr("pv_forecast.SOLCAST_PROXY_BASE_URL", "http://proxy.test")
    monkeypatch.setattr("pv_forecast.httpx.Client", FakeClient)

    now = datetime(2026, 7, 9, 12, 30, tzinfo=TZ)
    captured["history"] = [
        _hist_item(
            fetched_at="2026-07-09T09:55:01",
            period_end="2026-07-09T08:30:00.0000000Z",  # h10
            pv=3.5,
        ),
        _hist_item(
            fetched_at="2026-07-09T09:55:01",
            period_end="2026-07-09T09:00:00.0000000Z",  # h10
            pv=3.7,
        ),
    ]
    out = fetch_hourly_pv_forecast_with_history(
        hours_back=24,
        hours_forward=24,
        now=now,
    )
    params = captured.get("history_params") or {}
    assert params.get("start")
    assert params.get("end")
    assert params.get("tz") == "Europe/Warsaw"
    h10 = [h for h in out["hours"] if h["date"] == "2026-07-09" and h["hour"] == 10]
    assert len(h10) == 1
    assert h10[0]["pv_kw"] == pytest.approx(3.6, rel=0.01)


def test_in_progress_hour_falls_back_to_history_when_forecasts_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Po wypadnięciu slotów bieżącej godziny z /forecasts — snapshot sprzed :00."""

    class FakeResp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url: str, params: dict | None = None):
            if url.endswith("/forecasts"):
                # Live ma tylko godzinę 12+; sloty h11 już wypadły.
                # h12 CEST = period_end 10:30Z + 11:00Z
                return FakeResp(
                    {
                        "data": {
                            "forecasts": [
                                {
                                    "period_end": "2026-07-24T10:30:00.0000000Z",
                                    "pv_estimate": 6.0,
                                    "pv_estimate10": 4.8,
                                    "pv_estimate90": 7.2,
                                },
                                {
                                    "period_end": "2026-07-24T11:00:00.0000000Z",
                                    "pv_estimate": 6.2,
                                    "pv_estimate10": 5.0,
                                    "pv_estimate90": 7.4,
                                },
                            ]
                        },
                        "cached": False,
                        "fetched_at": "2026-07-24T11:52:00",
                    }
                )
            return FakeResp(
                {
                    "forecasts": [
                        _hist_item(
                            fetched_at="2026-07-24T10:55:01",
                            period_end="2026-07-24T09:30:00.0000000Z",  # h11
                            pv=5.0,
                        ),
                        _hist_item(
                            fetched_at="2026-07-24T10:55:01",
                            period_end="2026-07-24T10:00:00.0000000Z",  # h11
                            pv=5.4,
                        ),
                    ]
                }
            )

    monkeypatch.setattr("pv_forecast.SOLCAST_PROXY_BASE_URL", "http://proxy.test")
    monkeypatch.setattr("pv_forecast.httpx.Client", FakeClient)

    now = datetime(2026, 7, 24, 11, 52, tzinfo=TZ)
    out = fetch_hourly_pv_forecast_with_history(
        hours_back=2,
        hours_forward=2,
        now=now,
    )
    h11 = [h for h in out["hours"] if h["date"] == "2026-07-24" and h["hour"] == 11]
    assert len(h11) == 1
    assert h11[0]["pv_kw"] == pytest.approx(5.2, rel=0.01)
    h12 = [h for h in out["hours"] if h["date"] == "2026-07-24" and h["hour"] == 12]
    assert len(h12) == 1
    assert h12[0]["pv_kw"] == pytest.approx(6.1, rel=0.01)
