"""Wspólny rdzeń mid-hour: jsonl i mix k."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from planner.intra_hour import energy_in_hour, mix_k_into_hour, mix_or_scale_quantile, recent_average_kw


def test_energy_in_hour_uses_power_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("planner.intra_hour.TELEMETRY_DIR", tmp_path)
    path = tmp_path / "telemetry_2026-06-11.jsonl"
    rows = [
        {"local_hour": 12, "local_minute": 0, "pv_w": 1200.0, "consumption_w": 600.0},
        {"local_hour": 12, "local_minute": 1, "pv_w": 1200.0, "consumption_w": 600.0},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    pv = energy_in_hour(local_date="2026-06-11", hour=12, power_field="pv_w")
    load = energy_in_hour(local_date="2026-06-11", hour=12, power_field="consumption_w")
    assert pv is not None and load is not None
    assert pv[1] == 2
    assert load[1] == 2
    assert pv[0] == pytest.approx(2 * 1.2 / 60.0)
    assert load[0] == pytest.approx(2 * 0.6 / 60.0)


def test_recent_average_respects_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("planner.intra_hour.TELEMETRY_DIR", tmp_path)
    path = tmp_path / "telemetry_2026-06-11.jsonl"
    rows = [
        {"local_hour": 12, "local_minute": 0, "pv_w": 1000.0},
        {"local_hour": 12, "local_minute": 20, "pv_w": 3000.0},
        {"local_hour": 12, "local_minute": 25, "pv_w": 3000.0},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    now = datetime(2026, 6, 11, 12, 25, 0)
    got = recent_average_kw(now, window_min=10, power_field="pv_w")
    assert got is not None
    avg, n = got
    assert n == 2
    assert avg == pytest.approx(3.0)


def test_mix_k_into_hour_reexport_shape() -> None:
    assert mix_k_into_hour(f50=2.0, k=0.5, spill_min=30) == pytest.approx(1.5)


def test_mix_or_scale_quantile_mix_vs_scale() -> None:
    mixed = mix_or_scale_quantile(
        q_raw=1.0, f50_raw=2.0, k=0.5, spill_min=30, mix=True, corrected=1.6
    )
    assert mixed == mix_k_into_hour(f50=2.0, k=0.5, spill_min=30, q_raw=1.0)
    scaled = mix_or_scale_quantile(
        q_raw=1.0, f50_raw=2.0, k=0.5, spill_min=30, mix=False, corrected=1.6
    )
    assert scaled == pytest.approx(0.8)
