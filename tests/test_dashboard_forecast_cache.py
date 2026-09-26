"""Prognoza: przeterminowany cache wraca od razu, przebudowa nie blokuje żądania."""

from __future__ import annotations

import time

import guardian_dashboard as gd


def test_stale_combined_forecast_is_served(monkeypatch) -> None:
    payload = {"rows": [{"hour": 1}]}
    gd._combined_forecast_cache = (time.monotonic() - (gd._COMBINED_FRESH_S + 5), payload)
    gd._combined_refreshing = True
    calls: list[int] = []
    monkeypatch.setattr(
        gd,
        "_combined_forecast_payload",
        lambda: calls.append(1) or {"rows": [{"hour": 2}]},
    )
    got = gd._get_combined_forecast_cached()
    assert got is payload
    assert calls == []


def test_fresh_combined_forecast_skips_rebuild(monkeypatch) -> None:
    payload = {"rows": [{"hour": 3}]}
    gd._combined_forecast_cache = (time.monotonic(), payload)
    gd._combined_refreshing = False
    monkeypatch.setattr(
        gd,
        "_combined_forecast_payload",
        lambda: (_ for _ in ()).throw(AssertionError("rebuild")),
    )
    assert gd._get_combined_forecast_cached() is payload
