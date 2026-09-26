"""Zamknięte dni KPI są zapisywane i czytane bez ponownego liczenia."""

from __future__ import annotations

from datetime import date

import guardian_dashboard as gd


def test_past_day_totals_survive_memory_reset(monkeypatch, tmp_path) -> None:
    path = tmp_path / "kpi_daily_totals.json"
    monkeypatch.setattr(gd, "_kpi_totals_path", lambda: path)
    gd._kpi_disk = None
    gd._kpi_totals_cache = {}
    calls: list[date] = []

    def fake_kpi(d: date) -> dict:
        calls.append(d)
        return {"totals": {"deposit_add_pln_day": 3.0, "net_export_surplus_kwh": 6.0}}

    monkeypatch.setattr(gd, "_kpi_for_day", fake_kpi)
    day = date(2026, 1, 15)
    today = date(2026, 9, 26)
    first = gd._kpi_totals_for_day(day, today=today)
    assert first is not None
    assert calls == [day]
    assert path.is_file()

    gd._kpi_totals_cache = {}
    gd._kpi_disk = None
    second = gd._kpi_totals_for_day(day, today=today)
    assert second == first
    assert calls == [day]


def test_today_totals_are_not_persisted(monkeypatch, tmp_path) -> None:
    path = tmp_path / "kpi_daily_totals.json"
    monkeypatch.setattr(gd, "_kpi_totals_path", lambda: path)
    gd._kpi_disk = None
    gd._kpi_totals_cache = {}
    today = date(2026, 9, 26)
    monkeypatch.setattr(
        gd,
        "_kpi_for_day",
        lambda _d: {"totals": {"deposit_add_pln_day": 1.0, "net_export_surplus_kwh": 2.0}},
    )
    gd._kpi_totals_for_day(today, today=today)
    assert not path.exists()
