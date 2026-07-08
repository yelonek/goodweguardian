"""Testy load_base — EV nie skaża prognozy innych godzin."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from load_forecast import (
    _hourly_base_kwh_from_file,
    build_daily_hourly_kwh_cache,
    predict_load_one_hour,
)


def _write_telemetry_day(
    path: Path,
    *,
    local_date: date,
    hourly_load_kwh: dict[int, float],
    hourly_ev_kwh: dict[int, float] | None = None,
) -> None:
    """Minimalny JSONL: consumption_w i opcjonalnie E_twc_kwh per godzina."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ev = hourly_ev_kwh or {}
    e_twc = 1000.0
    lines: list[str] = []
    for h in range(24):
        if h not in hourly_load_kwh and h not in ev:
            continue
        load_w = hourly_load_kwh.get(h, 0.5) * 1000.0
        row: dict = {
            "local_date": local_date.isoformat(),
            "local_hour": h,
            "local_minute": 0,
            "consumption_w": load_w,
        }
        if h in ev or any(x in ev for x in range(h)):
            row["E_twc_kwh"] = e_twc
            e_twc += ev.get(h, 0.0)
        lines.append(json.dumps(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_hourly_base_subtracts_ev(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from guardian_config import TELEMETRY_DIR

    d = date(2026, 6, 7)  # Saturday
    monkeypatch.setattr("load_forecast.TELEMETRY_DIR", tmp_path)
    monkeypatch.setattr("tesla_wall_charger.TELEMETRY_DIR", tmp_path)

    p = tmp_path / f"telemetry_{d.isoformat()}.jsonl"
    _write_telemetry_day(
        p,
        local_date=d,
        hourly_load_kwh={7: 0.5, 10: 12.0, 11: 12.0, 12: 12.0},
        hourly_ev_kwh={10: 11.0, 11: 11.0, 12: 11.0},
    )

    base = _hourly_base_kwh_from_file(p, d)
    assert base[7] == pytest.approx(0.5)
    assert base[10] == pytest.approx(1.0)
    assert base[11] == pytest.approx(1.0)


def test_ev_day_does_not_inflate_other_hours(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("load_forecast.TELEMETRY_DIR", tmp_path)
    monkeypatch.setattr("tesla_wall_charger.TELEMETRY_DIR", tmp_path)

    target = date(2026, 6, 14)  # Saturday
    for i in range(1, 10):
        d = target - timedelta(days=i)
        p = tmp_path / f"telemetry_{d.isoformat()}.jsonl"
        if i == 1:
            # Sobota z EV 10-12
            _write_telemetry_day(
                p,
                local_date=d,
                hourly_load_kwh={7: 0.4, 10: 12.0, 11: 12.0, 12: 12.0},
                hourly_ev_kwh={10: 11.0, 11: 11.0, 12: 11.0},
            )
        else:
            _write_telemetry_day(
                p,
                local_date=d,
                hourly_load_kwh={7: 0.4, 10: 0.5, 11: 0.5, 12: 0.5},
            )

    cache = build_daily_hourly_kwh_cache()
    pred_7 = predict_load_one_hour(target, 7, lookback_days=28, cache=cache)
    pred_10 = predict_load_one_hour(target, 10, lookback_days=28, cache=cache)

    assert pred_7["load_kwh_p50"] == pytest.approx(0.4, abs=0.05)
    assert pred_10["load_kwh_p50"] < 2.0
