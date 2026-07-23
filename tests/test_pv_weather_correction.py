"""Testy eksperymentalnej korekty PV pogodą OWM (Tier1)."""

from __future__ import annotations

from datetime import datetime

import pytest

from planner.pv_weather_correction import (
    apply_pv_weather_correction,
    clearness_proxy,
    k_wx_from_tier1,
    weather_id_penalty,
)


def test_weather_id_penalty_fog_and_storm() -> None:
    assert weather_id_penalty(800) == 1.0
    assert weather_id_penalty(741) == pytest.approx(0.60)
    assert weather_id_penalty(211) == pytest.approx(0.45)


def test_clearness_overcast_lower_than_clear() -> None:
    clear = clearness_proxy(clouds=10, uvi=7.0, pop=0.0, weather_id=800)
    overcast = clearness_proxy(clouds=95, uvi=1.0, pop=0.4, weather_id=804)
    assert clear > overcast


def test_k_wx_relative_to_ref_clips() -> None:
    k_bad, meta = k_wx_from_tier1(
        clouds=100,
        uvi=0.2,
        pop=0.9,
        weather_id=502,
        rain_1h=3.0,
    )
    assert meta["k_wx"] == k_bad
    assert k_bad == pytest.approx(0.45)  # floor

    k_good, _ = k_wx_from_tier1(
        clouds=5,
        uvi=9.0,
        pop=0.0,
        weather_id=800,
    )
    assert k_good == pytest.approx(1.25)  # ceiling


def test_minutely_derate_only_when_flagged() -> None:
    k0, m0 = k_wx_from_tier1(
        clouds=40,
        uvi=4.0,
        pop=0.1,
        weather_id=801,
        minutely_mean_mmh=0.5,
        apply_minutely=False,
    )
    k1, m1 = k_wx_from_tier1(
        clouds=40,
        uvi=4.0,
        pop=0.1,
        weather_id=801,
        minutely_mean_mmh=0.5,
        apply_minutely=True,
    )
    assert not m0["minutely_applied"]
    assert m1["minutely_applied"]
    assert k1 < k0


def test_expand_forecast_3h_covers_three_local_hours() -> None:
    from datetime import timezone

    from weather_owm import expand_forecast_3h_to_hourly, hourly_by_local_slot

    # 2026-06-11 12:00 UTC → 14:00 Europe/Warsaw (CEST)
    dt = int(datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    rows = expand_forecast_3h_to_hourly(
        [
            {
                "dt": dt,
                "main": {"temp": 22.0},
                "clouds": {"all": 77},
                "pop": 0.4,
                "weather": [{"id": 803, "main": "Clouds", "description": "broken clouds"}],
                "rain": {"3h": 1.5},
            }
        ],
        tz_name="Europe/Warsaw",
    )
    keys = {(r["local_date"], r["local_hour"]) for r in rows}
    assert ("2026-06-11", 14) in keys
    assert ("2026-06-11", 15) in keys
    assert ("2026-06-11", 16) in keys
    assert len(keys) == 3
    pack = {"hourly": rows}
    by_hour = hourly_by_local_slot(pack, tz_name="Europe/Warsaw")
    assert by_hour[("2026-06-11", 14)]["clouds"] == 77
    assert by_hour[("2026-06-11", 14)]["rain_1h"] == pytest.approx(0.5)
    assert by_hour[("2026-06-11", 14)]["uvi"] is None


def test_apply_scales_h2_to_h6_skips_intra(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 6, 11, 10, 20, 0)
    slots = [
        ("2026-06-11", 10),
        ("2026-06-11", 11),
        ("2026-06-11", 12),
        ("2026-06-11", 13),
        ("2026-06-11", 16),
        ("2026-06-11", 17),  # h+7 — poza horyzontem
    ]
    pv_by_key = {
        s: {"pv_kw": 4.0, "pv_kw_p10": 3.0, "pv_kw_p90": 5.0} for s in slots
    }
    corrected = {s: 4.0 for s in slots}
    sources = {
        ("2026-06-11", 10): "pv_intra_current",
        ("2026-06-11", 11): "pv_intra_next",
        ("2026-06-11", 12): "solcast_proxy",
        ("2026-06-11", 13): "solcast_proxy",
        ("2026-06-11", 16): "solcast_proxy",
        ("2026-06-11", 17): "solcast_proxy",
    }

    def _hour_row(clouds: float, weather_id: int, pop: float = 0.0) -> dict:
        return {
            "clouds": clouds,
            "uvi": 5.0,
            "pop": pop,
            "weather_id": weather_id,
            "rain_1h": 0.0,
            "snow_1h": 0.0,
        }

    onecall = {
        "_meta": {"error": None, "cached": True},
        "current": {
            "dt": 1,
            "clouds": 80,
            "uvi": 3.0,
            "weather": [{"id": 803, "main": "Clouds", "description": "broken clouds"}],
        },
        "minutely": [{"precipitation": 0.0} for _ in range(60)],
        "hourly": [],
    }
    # Build hourly with unix dt matching local Europe/Warsaw summer (UTC+2)
    # 2026-06-11 12:00 Warsaw = 10:00 UTC
    from datetime import timezone

    for hour, clouds, wid in [
        (12, 90, 804),
        (13, 20, 800),
        (16, 50, 802),
        (17, 10, 800),
    ]:
        dt = datetime(2026, 6, 11, hour, 0, 0, tzinfo=timezone.utc).timestamp()
        # Actually we need local TELEMETRY_TZ mapping — weather_owm uses TELEMETRY_TZ.
        # Pass pre-shaped via monkeypatch of hourly_by_local_slot instead for stability.
        onecall["hourly"].append(
            {
                "dt": int(dt),
                "temp": 20,
                "clouds": clouds,
                "uvi": 5,
                "pop": 0.2 if clouds > 70 else 0.0,
                "weather": [{"id": wid, "main": "Clouds", "description": "x"}],
            }
        )

    import planner.pv_weather_correction as wx_mod

    monkeypatch.setattr(
        wx_mod,
        "hourly_by_local_slot",
        lambda _pack, **_kw: {
            ("2026-06-11", 12): _hour_row(90, 804, 0.5),
            ("2026-06-11", 13): _hour_row(10, 800, 0.0),
            ("2026-06-11", 16): _hour_row(50, 802, 0.1),
            ("2026-06-11", 17): _hour_row(10, 800, 0.0),
        },
    )
    monkeypatch.setattr(wx_mod, "minutely_mean_precip_mmh", lambda _p: 0.0)
    monkeypatch.setattr(wx_mod, "current_tier1", lambda _p: {"clouds": 80})
    monkeypatch.setattr(wx_mod, "owm_configured", lambda: True)

    new_corr, new_src, meta = apply_pv_weather_correction(
        slots,
        pv_by_key,
        corrected,
        sources,
        now=now,
        onecall=onecall,
        enabled=True,
    )

    assert meta["applied"] is True
    assert new_src[("2026-06-11", 10)] == "pv_intra_current"
    assert new_src[("2026-06-11", 11)] == "pv_intra_next"
    assert new_src[("2026-06-11", 12)] == "pv_weather_tier1"
    assert new_src[("2026-06-11", 13)] == "pv_weather_tier1"
    assert new_src[("2026-06-11", 16)] == "pv_weather_tier1"
    assert new_src[("2026-06-11", 17)] == "solcast_proxy"  # h+7
    assert new_corr[("2026-06-11", 12)] < 4.0  # pochmurno → w dół
    assert new_corr[("2026-06-11", 13)] >= 4.0  # jasno → w górę (do clip)


def test_apply_skips_when_disabled() -> None:
    now = datetime(2026, 6, 11, 10, 0, 0)
    slots = [("2026-06-11", 12)]
    corrected = {slots[0]: 3.0}
    sources = {slots[0]: "solcast_proxy"}
    out_c, out_s, meta = apply_pv_weather_correction(
        slots,
        {slots[0]: {"pv_kw": 3.0}},
        corrected,
        sources,
        now=now,
        enabled=False,
    )
    assert meta["reason"] == "disabled"
    assert out_c[slots[0]] == 3.0
    assert out_s[slots[0]] == "solcast_proxy"
