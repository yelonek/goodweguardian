"""Testy piramidy PV × RCE (pv_pyramid.py)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from pv_pyramid import PV_PYRAMID_TIERS_GR, build_pv_pyramid_payload


def _pricing_day(*, rce_by_hour: dict[int, float]) -> dict:
    hours = [
        {
            "hour": h,
            "import_pln_per_kwh": 0.59,
            "rce_pln_kwh": rce,
        }
        for h, rce in sorted(rce_by_hour.items())
    ]
    return {"source": "test", "hours": hours}


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 6, 20, 14, 30, 0)


def test_pyramid_actual_past_forecast_future(fixed_now: datetime) -> None:
    today = fixed_now.date().isoformat()
    tomorrow = "2026-06-21"

    def pricing(local_date):
        if str(local_date) == today:
            return _pricing_day(rce_by_hour={h: 0.25 for h in range(24)})
        if str(local_date) == tomorrow:
            return _pricing_day(rce_by_hour={h: 0.70 for h in range(24)})
        return None

    pv_hours = []
    for h in range(24):
        pv_hours.append({"date": today, "hour": h, "pv_kw": 1.0})
    for h in range(24):
        pv_hours.append({"date": tomorrow, "hour": h, "pv_kw": 2.0})

    with (
        patch("pv_pyramid.pricing_day_breakdown", side_effect=pricing),
        patch("pv_pyramid.fetch_hourly_pv_forecast_with_history", return_value={"hours": pv_hours}),
        patch(
            "guardian_dashboard._telemetry_hourly_load_pv_actuals",
            side_effect=lambda d: (
                {},
                {h: 0.5 for h in range(15)} if d.isoformat() == today else {},
            ),
        ),
        patch("guardian_dashboard._pricing_for_day_quiet", side_effect=pricing),
    ):
        p = build_pv_pyramid_payload(now=fixed_now)

    assert p["pv_total_kwh"] == pytest.approx(14 * 0.5 + 10 * 1.0 + 24 * 2.0)
    assert p["tiers"][-1]["threshold_gr"] == 60
    assert p["tiers"][-1]["cumulative_kwh"] == pytest.approx(14 * 0.5 + 10 * 1.0)
    assert p["above_60_kwh"] == pytest.approx(24 * 2.0)

    seg = p["segments"]
    assert seg["cheap_threshold_gr"] == 59
    assert seg["today"]["past"]["cheap_kwh"] == pytest.approx(14 * 0.5)
    assert seg["today"]["past"]["pv_total_kwh"] == pytest.approx(14 * 0.5)
    assert seg["today"]["remaining"]["cheap_kwh"] == pytest.approx(10 * 1.0)
    assert seg["today"]["remaining"]["pv_total_kwh"] == pytest.approx(10 * 1.0)
    assert seg["today"]["total"]["cheap_kwh"] == pytest.approx(14 * 0.5 + 10 * 1.0)
    assert seg["tomorrow"]["total"]["above_60_kwh"] == pytest.approx(24 * 2.0)
    assert seg["tomorrow"]["total"]["cheap_kwh"] == pytest.approx(0.0)


def test_pyramid_tier_layers_incremental(fixed_now: datetime) -> None:
    today = fixed_now.date().isoformat()

    def pricing(local_date):
        if str(local_date) == today:
            return _pricing_day(rce_by_hour={0: 0.05, 1: 0.15, 2: 0.55})
        return _pricing_day(rce_by_hour={})

    pv_hours = [{"date": today, "hour": h, "pv_kw": 1.0} for h in range(3)]

    with (
        patch("pv_pyramid.pricing_day_breakdown", side_effect=pricing),
        patch("pv_pyramid.fetch_hourly_pv_forecast_with_history", return_value={"hours": pv_hours}),
        patch(
            "guardian_dashboard._telemetry_hourly_load_pv_actuals",
            return_value=({}, {}),
        ),
        patch("guardian_dashboard._pricing_for_day_quiet", side_effect=pricing),
    ):
        p = build_pv_pyramid_payload(now=datetime(2026, 6, 20, 4, 0, 0))

    by_gr = {t["threshold_gr"]: t for t in p["tiers"]}
    assert by_gr[10]["cumulative_kwh"] == pytest.approx(1.0)
    assert by_gr[20]["cumulative_kwh"] == pytest.approx(2.0)
    assert by_gr[60]["cumulative_kwh"] == pytest.approx(3.0)
    assert by_gr[20]["layer_kwh"] == pytest.approx(1.0)
    assert p["above_60_kwh"] == pytest.approx(0.0)


def test_pyramid_tiers_count() -> None:
    assert len(PV_PYRAMID_TIERS_GR) == 6


def test_pyramid_cheap_surplus_after_load(fixed_now: datetime) -> None:
    """Tanio po load_base p50: PV − load (bez planu) w godzinach RCE < 59 gr."""
    today = fixed_now.date().isoformat()

    def pricing(local_date):
        return _pricing_day(rce_by_hour={h: 0.25 for h in range(24)})

    pv_hours = [{"date": today, "hour": h, "pv_kw": 2.0} for h in range(14, 24)]

    def load_hours(**_kwargs):
        return {
            "hours": [
                {
                    "date": today,
                    "hour": h,
                    "load_kwh_p50": 0.5,
                    "load_base_kwh_p50": 0.5,
                }
                for h in range(14, 24)
            ]
        }

    with (
        patch("pv_pyramid.pricing_day_breakdown", side_effect=pricing),
        patch("guardian_dashboard._pricing_for_day_quiet", side_effect=pricing),
        patch("pv_pyramid.fetch_hourly_pv_forecast_with_history", return_value={"hours": pv_hours}),
        patch("pv_pyramid.forecast_load_hours", side_effect=load_hours),
        patch("guardian_dashboard._telemetry_hourly_load_pv_actuals", return_value=({}, {})),
        patch("planner.plan_store.load_latest_plan", return_value=None),
        patch("tesla_wall_charger.twc_enabled", return_value=False),
    ):
        p = build_pv_pyramid_payload(now=fixed_now)

    remaining = p["segments"]["today"]["remaining"]
    assert remaining["cheap_kwh"] == pytest.approx(10 * 2.0)
    assert remaining["cheap_surplus_kwh"] == pytest.approx(10 * 1.5)
    assert remaining["load_base_kwh"] == pytest.approx(10 * 0.5)
