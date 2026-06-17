"""Testy automatycznej kalibracji λ (CVaR)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from planner.cvar_calibrate import (
    calibrate_cvar_lambda,
    get_effective_cvar_lambda,
    save_calibration_cache,
    CvarCalibrationResult,
)
import planner.cvar_calibrate as cal_mod
import planner.config as cfg_mod


def test_calibrate_picks_best_mean_score(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg_mod, "PLANNER_CVAR_CALIBRATE_MIN_DAYS", 2)
    monkeypatch.setattr(cfg_mod, "PLANNER_CVAR_CALIBRATE_GRID", [0.0, 1.0, 3.0])

    scores = {
        0.0: 5.0,
        1.0: 6.2,
        3.0: 5.8,
    }

    def fake_backtest(local_date: date, *, lambda_value: float, params) -> float | None:
        _ = (local_date, params)
        return scores.get(lambda_value)

    monkeypatch.setattr(cal_mod, "_telemetry_dates_in_lookback", lambda **_: [date(2026, 6, 1), date(2026, 6, 2)])
    monkeypatch.setattr(cal_mod, "_backtest_lambda_on_day", fake_backtest)

    result = calibrate_cvar_lambda(end_date=date(2026, 6, 3))
    assert result.lambda_value == pytest.approx(1.0)
    assert result.days_used == 2
    assert result.method == "backtest_mean_cashflow"


def test_get_effective_cvar_lambda_auto_uses_cache(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cvar_calibration.json"
    monkeypatch.setattr(cal_mod, "CALIBRATION_CACHE_PATH", cache)
    monkeypatch.setattr(cfg_mod, "_CVAR_LAMBDA_RAW", "auto")
    monkeypatch.setattr(cfg_mod, "PLANNER_CVAR_CALIBRATE_CACHE_HOURS", 48)

    save_calibration_cache(
        CvarCalibrationResult(
            lambda_value=1.25,
            calibrated_at=datetime.now(UTC).isoformat(),
            lookback_days=28,
            days_used=10,
            scores_by_lambda={"1.25": 3.5},
            method="backtest_mean_cashflow",
            notes=[],
        )
    )

    assert get_effective_cvar_lambda() == pytest.approx(1.25)


def test_fallback_lambda_when_insufficient_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg_mod, "PLANNER_CVAR_CALIBRATE_MIN_DAYS", 99)
    monkeypatch.setattr(cfg_mod, "PLANNER_CVAR_CALIBRATE_DEFAULT_LAMBDA", 1.75)
    monkeypatch.setattr(cal_mod, "_telemetry_dates_in_lookback", lambda **_: [])

    result = calibrate_cvar_lambda(end_date=date(2026, 6, 14))
    assert result.method == "fallback"
    assert result.lambda_value == pytest.approx(1.75)
