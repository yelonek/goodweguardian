"""Prognoza łączona: kolumny EV / dom z TWC."""

from __future__ import annotations

from datetime import date

import pytest


def test_combined_forecast_includes_twc_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from guardian_dashboard import _combined_forecast_payload

    today = date.today()
    today_iso = today.isoformat()

    monkeypatch.setattr("guardian_dashboard.twc_enabled", lambda: True)
    monkeypatch.setattr(
        "guardian_dashboard.hourly_ev_kwh_from_telemetry",
        lambda d: {12: 1.5} if d.isoformat() == today_iso else {},
    )

    def fake_load_pv(local_date: date):
        load = {12: 3.2} if local_date.isoformat() == today_iso else {}
        return load, {}

    monkeypatch.setattr(
        "guardian_dashboard._telemetry_hourly_load_pv_actuals",
        fake_load_pv,
    )

    payload = _combined_forecast_payload()
    assert payload["twc_enabled"] is True
    row = next(r for r in payload["rows"] if r["date"] == today_iso and r["hour"] == 12)
    if row["hour_complete"]:
        assert row["ev_kwh_actual"] == pytest.approx(1.5)
        assert row["load_base_kwh_actual"] == pytest.approx(1.7)
    else:
        assert row["ev_kwh_actual"] is None
        assert row["load_base_kwh_actual"] is None
