"""Testy archiwum cech PV (Solcast + OWM) pod ML."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from planner.pv_feature_archive import (
    DayFeatureArchive,
    HourFeatureRow,
    archive_pv_features,
    enrich_actuals_for_date,
    load_day_archive,
)


def test_archive_updates_future_then_freezes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "planner.pv_feature_archive.PLANNER_PV_FEATURES_DIR",
        tmp_path,
    )
    monkeypatch.setattr("planner.pv_feature_archive.ensure_planner_dirs", lambda: None)
    monkeypatch.setattr("planner.pv_feature_archive.owm_configured", lambda: True)

    wx_pack = {
        "_meta": {"source": "test_owm", "error": None},
        "hourly": [
            {
                "local_date": "2026-08-09",
                "local_hour": 15,
                "temp": 22.0,
                "clouds": 40.0,
                "pop": 0.1,
                "weather_id": 801,
                "weather_main": "Clouds",
                "weather_description": "few clouds",
                "rain_1h": 0.0,
                "snow_1h": 0.0,
                "visibility": 10000,
                "from_3h": True,
                "from_current": False,
            }
        ],
    }
    monkeypatch.setattr(
        "planner.pv_feature_archive.fetch_weather_pack",
        lambda **_k: wx_pack,
    )

    pv_by = {
        ("2026-08-09", 15): {
            "pv_kw": 4.0,
            "pv_kw_p10": 2.0,
            "pv_kw_p90": 5.0,
            "source": "solcast_proxy",
        }
    }

    # 13:00 — slot 15 jeszcze przyszły, upsert #1
    s1 = archive_pv_features(
        now=datetime(2026, 8, 9, 13, 0, 0),
        pv_by_key=pv_by,
        slots=[("2026-08-09", 15)],
        weather_pack=wx_pack,
    )
    assert s1["rows_written"] == 1
    day = load_day_archive("2026-08-09")
    assert day is not None
    row = day.hours["15"]
    assert row.frozen is False
    assert row.solcast_p50_kwh == 4.0
    assert row.owm_clouds == 40.0
    assert row.updates == 1

    # 13:40 — świeższy Solcast, nadal nie zamrożone
    pv_by[("2026-08-09", 15)] = {
        "pv_kw": 4.5,
        "pv_kw_p10": 2.2,
        "pv_kw_p90": 5.2,
        "source": "solcast_proxy",
    }
    archive_pv_features(
        now=datetime(2026, 8, 9, 13, 40, 0),
        pv_by_key=pv_by,
        slots=[("2026-08-09", 15)],
        weather_pack=wx_pack,
    )
    day = load_day_archive("2026-08-09")
    assert day is not None
    row = day.hours["15"]
    assert row.frozen is False
    assert row.solcast_p50_kwh == 4.5
    assert row.updates == 2

    # 15:10 — zamrożenie; zostaje ostatni pre-hour (4.5), nie bierze 9.0
    pv_by[("2026-08-09", 15)] = {
        "pv_kw": 9.0,
        "pv_kw_p10": 8.0,
        "pv_kw_p90": 9.5,
        "source": "solcast_proxy",
    }
    archive_pv_features(
        now=datetime(2026, 8, 9, 15, 10, 0),
        pv_by_key=pv_by,
        slots=[("2026-08-09", 15)],
        weather_pack=wx_pack,
    )
    day = load_day_archive("2026-08-09")
    assert day is not None
    row = day.hours["15"]
    assert row.frozen is True
    assert row.solcast_p50_kwh == 4.5
    assert row.owm_temp_c == 22.0


def test_archive_attaches_meter_actual_after_hour(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "planner.pv_feature_archive.PLANNER_PV_FEATURES_DIR",
        tmp_path,
    )
    monkeypatch.setattr("planner.pv_feature_archive.ensure_planner_dirs", lambda: None)
    monkeypatch.setattr("planner.pv_feature_archive.owm_configured", lambda: False)
    monkeypatch.setattr(
        "planner.pv_feature_archive.hourly_pv_meter_delta_kwh",
        lambda _d: {14: 3.25},
    )
    monkeypatch.setattr(
        "planner.pv_feature_archive.hourly_actuals",
        lambda _d: {14: {"pv_kwh": 3.1, "samples": 60}},
    )

    pv_by = {
        ("2026-08-09", 14): {
            "pv_kw": 3.0,
            "pv_kw_p10": 1.5,
            "pv_kw_p90": 4.0,
        }
    }
    # Pre-hour
    archive_pv_features(
        now=datetime(2026, 8, 9, 13, 50, 0),
        pv_by_key=pv_by,
        slots=[("2026-08-09", 14)],
    )
    # After hour end — attach actual
    summary = archive_pv_features(
        now=datetime(2026, 8, 9, 15, 5, 0),
        pv_by_key=pv_by,
        slots=[("2026-08-09", 14)],
    )
    assert summary["actuals_attached"] >= 1
    day = load_day_archive("2026-08-09")
    assert day is not None
    row = day.hours["14"]
    assert row.frozen is True
    assert row.pv_actual_kwh == 3.25
    assert row.pv_actual_method == "meter_delta_E_pv"
    assert row.pv_actual_power_avg_kwh == 3.1


def test_enrich_actuals_for_date(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "planner.pv_feature_archive.PLANNER_PV_FEATURES_DIR",
        tmp_path,
    )
    monkeypatch.setattr("planner.pv_feature_archive.ensure_planner_dirs", lambda: None)
    monkeypatch.setattr(
        "planner.pv_feature_archive.hourly_pv_meter_delta_kwh",
        lambda _d: {10: 1.1},
    )
    monkeypatch.setattr(
        "planner.pv_feature_archive.hourly_actuals",
        lambda _d: {},
    )

    archive = DayFeatureArchive(
        date="2026-08-08",
        updated_at_local="2026-08-08T10:00:00",
        hours={
            "10": HourFeatureRow(
                date="2026-08-08",
                hour=10,
                as_of_local="2026-08-08T09:50:00",
                frozen=True,
                solcast_p50_kwh=2.0,
            )
        },
    )
    from planner.pv_feature_archive import save_day_archive

    save_day_archive(archive)
    result = enrich_actuals_for_date(
        "2026-08-08",
        now=datetime(2026, 8, 8, 12, 0, 0),
    )
    assert result["ok"] is True
    assert result["actuals_attached"] == 1
    day = load_day_archive("2026-08-08")
    assert day is not None
    assert day.hours["10"].pv_actual_kwh == 1.1
