"""Testy korekty względnej nowcast w load_forecast."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from load_forecast import _apply_nowcast_to_rows


def test_nowcast_relative_factor_scales_not_zeroes() -> None:
    """Niski recent vs wysoki baseline nie powinien zerować niskiego p50 kolejnej godziny."""
    rows = [
        {
            "date": "2026-06-11",
            "hour": 14,
            "load_kwh_p25": 1.0,
            "load_kwh_p50": 2.0,
            "load_kwh_p75": 3.0,
            "samples": 10,
            "source": "weekday_weekend_hour",
        },
        {
            "date": "2026-06-11",
            "hour": 15,
            "load_kwh_p25": 0.5,
            "load_kwh_p50": 0.8,
            "load_kwh_p75": 1.2,
            "samples": 10,
            "source": "weekday_weekend_hour",
        },
    ]
    now = datetime(2026, 6, 11, 14, 30, 0)
    cache = {now.date() - timedelta(days=1): {14: 2.0, 15: 0.8}}

    with patch("load_forecast.predict_load_one_hour") as pred_mock, patch(
        "load_forecast.recent_consumption_average_w", return_value=920.0
    ):
        pred_mock.return_value = {
            "load_kwh_p50": 2.6,
            "samples": 10,
            "source": "weekday_weekend_hour",
        }
        out_rows, meta = _apply_nowcast_to_rows(
            rows, now=now, lookback_days=28, cache=cache
        )

    assert meta["applied"] is True
    assert meta["factor"] == 0.65  # clip: 920/2600 ≈ 0.35 → 0.65
    h15 = out_rows[1]
    assert h15["load_kwh_p50"] > 0.0
    assert h15["load_kwh_p50"] == 0.8 * (1.0 + (0.65 - 1.0) * 0.75)


def test_nowcast_skipped_when_disabled() -> None:
    rows = [{"load_kwh_p50": 1.0, "load_kwh_p25": 1.0, "load_kwh_p75": 1.0}]
    with patch("load_forecast.LOAD_NOWCAST_ENABLED", False):
        out_rows, meta = _apply_nowcast_to_rows(
            rows, now=datetime(2026, 6, 11, 12, 0), lookback_days=28, cache={}
        )
    assert meta["reason"] == "disabled"
    assert out_rows == rows
