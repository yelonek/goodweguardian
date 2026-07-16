"""Testy zwężania pasm load mid-hour."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from planner.load_correction import (
    load_energy_so_far_in_hour,
    load_remainder_bands_kwh,
)


def test_load_remainder_bands_alpha_zero_centered_on_p50() -> None:
    p50, p25, p75 = load_remainder_bands_kwh(
        p50_full=5.0,
        p25_full=2.0,
        p75_full=6.0,
        a_so_far=0.0,
        alpha=0.0,
    )
    assert p50 == pytest.approx(5.0)
    assert p25 == pytest.approx(3.0)
    assert p75 == pytest.approx(7.0)


def test_load_remainder_bands_alpha_one_collapses() -> None:
    p50, p25, p75 = load_remainder_bands_kwh(
        p50_full=5.0,
        p25_full=2.0,
        p75_full=6.0,
        a_so_far=4.0,
        alpha=1.0,
    )
    assert p50 == pytest.approx(1.0)
    assert p25 == pytest.approx(1.0)
    assert p75 == pytest.approx(1.0)


def test_load_remainder_bands_so_far_above_p25() -> None:
    """A > p25_full: p25 reszty > 0 dzięki zwężaniu + recent_kw."""
    p50, p25, p75 = load_remainder_bands_kwh(
        p50_full=5.3,
        p25_full=2.4,
        p75_full=5.5,
        a_so_far=3.5,
        alpha=2.0 / 3.0,
        recent_kw=4.5,
    )
    assert p25 > 0.0
    assert p25 <= p50 <= p75
    assert p25 > 1.0


def test_load_remainder_bands_kill_switch_uses_subtract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import planner.load_correction as load_mod

    monkeypatch.setattr(load_mod, "LOAD_BAND_NARROW_ENABLED", False)
    p50, p25, p75 = load_remainder_bands_kwh(
        p50_full=5.3,
        p25_full=2.4,
        p75_full=5.5,
        a_so_far=3.5,
        alpha=2.0 / 3.0,
        recent_kw=4.5,
    )
    assert p50 == pytest.approx(1.8)
    assert p25 == pytest.approx(0.0)
    assert p75 == pytest.approx(2.0)


def test_load_energy_so_far_in_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("planner.load_correction.TELEMETRY_DIR", tmp_path)
    path = tmp_path / "telemetry_2026-06-11.jsonl"
    rows = [
        {"local_hour": 11, "local_minute": 0, "consumption_w": 1200},
        {"local_hour": 11, "local_minute": 1, "consumption_w": 1300},
        {"local_hour": 11, "local_minute": 2, "consumption_w": 1250},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    now = datetime(2026, 6, 11, 11, 2, 30)
    got = load_energy_so_far_in_hour(now)
    assert got is not None
    energy, samples = got
    assert samples == 3
    assert energy == pytest.approx((1200 + 1300 + 1250) / 1000.0 / 60.0)
