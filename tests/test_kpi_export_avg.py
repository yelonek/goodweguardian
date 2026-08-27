"""Średnia zrealizowana cena sprzedaży (depozyt / kWh netto) — dzień i okresy."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

import guardian_dashboard as gd


def _pricing_flat(rce: float = 0.5, imp: float = 1.0) -> dict:
    return {
        "source": "test",
        "hours": [
            {"hour": h, "rce_pln_kwh": rce, "import_pln_per_kwh": imp} for h in range(24)
        ],
    }


def _net_hours(by_hour: dict[int, dict]) -> tuple[dict[int, dict], list[str]]:
    out: dict[int, dict] = {}
    for h in range(24):
        if h in by_hour:
            row = dict(by_hour[h])
            row.setdefault("delta_imp_kwh", 0.0)
            row.setdefault("delta_exp_kwh", 0.0)
            row.setdefault("complete", True)
            out[h] = row
        else:
            out[h] = {
                "net_kwh": 0.0,
                "delta_imp_kwh": 0.0,
                "delta_exp_kwh": 0.0,
                "complete": True,
            }
    return out, []


def test_iso_week_bounds_thursday() -> None:
    assert gd._iso_week_bounds(date(2026, 8, 27)) == (
        date(2026, 8, 24),
        date(2026, 8, 30),
    )


def test_month_bounds_december() -> None:
    assert gd._month_bounds(date(2026, 12, 15)) == (
        date(2026, 12, 1),
        date(2026, 12, 31),
    )


def test_year_bounds() -> None:
    assert gd._year_bounds(date(2026, 8, 27)) == (
        date(2026, 1, 1),
        date(2026, 12, 31),
    )


def test_kpi_for_day_includes_avg_export(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gd, "_read_telemetry_day", lambda _d: [{}])
    monkeypatch.setattr(gd, "pricing_day_breakdown", lambda _d: _pricing_flat(0.5))

    def fake_net(*, day):
        return _net_hours(
            {
                10: {"net_kwh": 4.0, "complete": True},
                11: {"net_kwh": -1.0, "complete": True},
            }
        )

    monkeypatch.setattr(gd, "_hourly_counter_net_kwh", fake_net)
    kpi = gd._kpi_for_day(date(2026, 8, 1))
    totals = kpi["totals"]
    assert totals["net_export_surplus_kwh"] == pytest.approx(4.0)
    assert totals["deposit_add_pln_day"] == pytest.approx(2.0)
    assert totals["avg_export_pln_per_kwh"] == pytest.approx(0.5)


def test_kpi_for_day_ignores_incomplete_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gd, "_read_telemetry_day", lambda _d: [{}])
    monkeypatch.setattr(gd, "pricing_day_breakdown", lambda _d: _pricing_flat(1.0))

    def fake_net(*, day):
        return _net_hours(
            {10: {"net_kwh": 100.0, "complete": False}},
        )

    monkeypatch.setattr(gd, "_hourly_counter_net_kwh", fake_net)
    kpi = gd._kpi_for_day(date(2026, 8, 1))
    assert kpi["totals"]["net_export_surplus_kwh"] == pytest.approx(0.0)
    assert kpi["totals"]["avg_export_pln_per_kwh"] is None


def test_kpi_for_day_no_export_avg_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gd, "_read_telemetry_day", lambda _d: [{}])
    monkeypatch.setattr(gd, "pricing_day_breakdown", lambda _d: _pricing_flat(0.5))

    def fake_net(*, day):
        return _net_hours({10: {"net_kwh": -2.0, "complete": True}})

    monkeypatch.setattr(gd, "_hourly_counter_net_kwh", fake_net)
    kpi = gd._kpi_for_day(date(2026, 8, 1))
    assert kpi["totals"]["avg_export_pln_per_kwh"] is None


def test_volume_weighted_not_mean_of_means(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gd, "_kpi_totals_cache", {})
    days = {
        date(2026, 8, 24): {
            "deposit_add_pln_day": 16.0,
            "net_export_surplus_kwh": 20.0,
        },
        date(2026, 8, 25): {
            "deposit_add_pln_day": 1.5,
            "net_export_surplus_kwh": 1.0,
        },
    }
    monkeypatch.setattr(gd, "_telemetry_day_exists", lambda d: d in days)
    monkeypatch.setattr(gd, "_kpi_for_day", lambda d: {"totals": days[d]})

    block = gd._aggregate_export_avg(
        date(2026, 8, 24), date(2026, 8, 30), today=date(2026, 8, 27)
    )
    assert block["from"] == "2026-08-24"
    assert block["to"] == "2026-08-27"
    assert block["days_used"] == 2
    assert block["avg_export_pln_per_kwh"] == pytest.approx(17.5 / 21.0)
    assert block["avg_export_pln_per_kwh"] != pytest.approx((0.8 + 1.5) / 2)


def test_skip_days_without_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gd, "_kpi_totals_cache", {})
    monkeypatch.setattr(gd, "_telemetry_day_exists", lambda _d: False)

    def boom(_d):
        raise AssertionError("nie wołamy RCE/KPI bez pliku telemetrii")

    monkeypatch.setattr(gd, "_kpi_for_day", boom)
    block = gd._aggregate_export_avg(
        date(2026, 8, 1), date(2026, 8, 31), today=date(2026, 8, 27)
    )
    assert block["days_used"] == 0
    assert block["avg_export_pln_per_kwh"] is None
    assert block["from"] == "2026-08-01"
    assert block["to"] == "2026-08-27"


def test_future_period_empty() -> None:
    block = gd._aggregate_export_avg(
        date(2026, 8, 28), date(2026, 8, 30), today=date(2026, 8, 27)
    )
    assert block["days_used"] == 0
    assert block["avg_export_pln_per_kwh"] is None
    assert block["from"] == "2026-08-28"
    assert block["to"] == "2026-08-30"


def test_aggregate_skips_kpi_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gd, "_kpi_totals_cache", {})
    good = date(2026, 8, 24)
    bad = date(2026, 8, 25)
    monkeypatch.setattr(gd, "_telemetry_day_exists", lambda d: d in {good, bad})

    def fake_kpi(d: date) -> dict:
        if d == bad:
            raise ValueError("oczekiwano 4 kwartałów dla godziny 2, jest 1")
        return {
            "totals": {
                "deposit_add_pln_day": 8.0,
                "net_export_surplus_kwh": 10.0,
            }
        }

    monkeypatch.setattr(gd, "_kpi_for_day", fake_kpi)
    block = gd._aggregate_export_avg(
        date(2026, 8, 24), date(2026, 8, 30), today=date(2026, 8, 27)
    )
    assert block["days_used"] == 1
    assert block["avg_export_pln_per_kwh"] == pytest.approx(0.8)
    assert block["net_export_surplus_kwh"] == pytest.approx(10.0)


def test_kpi_totals_cache_past_days(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[date] = []

    def fake_kpi(d: date) -> dict:
        calls.append(d)
        return {
            "totals": {"deposit_add_pln_day": 1.0, "net_export_surplus_kwh": 2.0}
        }

    monkeypatch.setattr(gd, "_kpi_for_day", fake_kpi)
    monkeypatch.setattr(gd, "_kpi_totals_cache", {})
    d = date(2026, 8, 1)
    today = date(2026, 8, 27)
    gd._kpi_totals_for_day(d, today=today)
    gd._kpi_totals_for_day(d, today=today)
    assert calls == [d]


def test_kpi_totals_cache_today_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([100.0, 110.0, 200.0])
    monkeypatch.setattr(gd.time, "monotonic", lambda: next(times))
    calls: list[date] = []

    def fake_kpi(d: date) -> dict:
        calls.append(d)
        return {
            "totals": {"deposit_add_pln_day": 1.0, "net_export_surplus_kwh": 2.0}
        }

    monkeypatch.setattr(gd, "_kpi_for_day", fake_kpi)
    monkeypatch.setattr(gd, "_kpi_totals_cache", {})
    today = date(2026, 8, 27)
    gd._kpi_totals_for_day(today, today=today)
    gd._kpi_totals_for_day(today, today=today)
    gd._kpi_totals_for_day(today, today=today)
    assert calls == [today, today]


def test_export_avg_payload_periods(monkeypatch: pytest.MonkeyPatch) -> None:
    today = date(2026, 8, 27)
    monkeypatch.setattr(gd, "_telemetry_today", lambda: today)
    monkeypatch.setattr(gd, "_kpi_totals_cache", {})
    monkeypatch.setattr(gd, "_telemetry_day_exists", lambda d: d == today)
    monkeypatch.setattr(
        gd,
        "_kpi_for_day",
        lambda _d: {
            "totals": {
                "deposit_add_pln_day": 4.2,
                "net_export_surplus_kwh": 10.0,
            }
        },
    )
    payload = gd._export_avg_payload(today)
    assert payload["day"]["avg_export_pln_per_kwh"] == pytest.approx(0.42)
    assert payload["day"]["from"] == "2026-08-27"
    assert payload["week"]["from"] == "2026-08-24"
    assert payload["week"]["to"] == "2026-08-27"
    assert payload["week"]["days_used"] == 1
    assert payload["month"]["from"] == "2026-08-01"
    assert payload["month"]["to"] == "2026-08-27"
    assert payload["year"]["from"] == "2026-01-01"
    assert payload["year"]["to"] == "2026-08-27"


def test_api_kpi_export_avg(monkeypatch: pytest.MonkeyPatch) -> None:
    d = date(2026, 8, 27)
    fake = {
        "day": {
            "from": d.isoformat(),
            "to": d.isoformat(),
            "avg_export_pln_per_kwh": 0.42,
            "deposit_add_pln": 12.3,
            "net_export_surplus_kwh": 29.3,
            "days_used": 1,
        },
        "week": {
            "from": "2026-08-24",
            "to": d.isoformat(),
            "avg_export_pln_per_kwh": 0.40,
            "deposit_add_pln": 20.0,
            "net_export_surplus_kwh": 50.0,
            "days_used": 3,
        },
        "month": {
            "from": "2026-08-01",
            "to": d.isoformat(),
            "avg_export_pln_per_kwh": 0.38,
            "deposit_add_pln": 100.0,
            "net_export_surplus_kwh": 263.0,
            "days_used": 20,
        },
        "year": {
            "from": "2026-01-01",
            "to": d.isoformat(),
            "avg_export_pln_per_kwh": 0.35,
            "deposit_add_pln": 400.0,
            "net_export_surplus_kwh": 1142.0,
            "days_used": 100,
        },
    }
    monkeypatch.setattr(gd, "_export_avg_payload", lambda local_date: fake)
    client = TestClient(gd.app)
    resp = client.get(f"/api/kpi/export-avg?day={d.isoformat()}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["day"]["avg_export_pln_per_kwh"] == pytest.approx(0.42)
    assert body["year"]["days_used"] == 100
    assert set(body) == {"day", "week", "month", "year"}
