"""Testy korekty PV (k_intra)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from planner.pv_correction import (
    apply_pv_correction,
    compute_k_intra,
    hour_elapsed_fraction,
    pv_energy_so_far_in_hour,
    pv_plan_current_hour_kwh,
    pv_plan_next_hour_kwh,
)


def test_hour_elapsed_fraction() -> None:
    assert hour_elapsed_fraction(datetime(2026, 6, 11, 11, 0, 0)) == pytest.approx(0.0)
    assert hour_elapsed_fraction(datetime(2026, 6, 11, 11, 30, 0)) == pytest.approx(0.5)
    assert hour_elapsed_fraction(datetime(2026, 6, 11, 11, 30, 30)) == pytest.approx(0.508333, rel=1e-4)


def test_compute_k_intra_example_1130() -> None:
    k, reason = compute_k_intra(
        f50_kwh=2.0,
        a_so_far_kwh=0.125,
        alpha=0.5,
        eps_kwh_per_h=0.1,
        k_min=0.65,
        k_max=1.35,
    )
    assert reason == "ok"
    assert k == pytest.approx(0.65)


def test_compute_k_intra_below_eps() -> None:
    k, reason = compute_k_intra(
        f50_kwh=0.05,
        a_so_far_kwh=0.01,
        alpha=0.1,
        eps_kwh_per_h=0.1,
    )
    assert k is None
    assert reason == "f_elapsed_below_eps"


def test_compute_k_intra_hour_start() -> None:
    k, reason = compute_k_intra(f50_kwh=2.0, a_so_far_kwh=0.0, alpha=0.0)
    assert k is None
    assert reason == "hour_start"


def test_pv_plan_current_and_next_hour() -> None:
    assert pv_plan_current_hour_kwh(
        f50_kwh=2.0,
        a_so_far_kwh=0.125,
        alpha=0.5,
        k_intra=0.65,
    ) == pytest.approx(0.775)
    assert pv_plan_next_hour_kwh(f50_kwh=2.0, k_intra=0.65) == pytest.approx(1.3)


def test_apply_pv_correction_adjusts_current_and_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import planner.pv_correction as pv_mod

    now = datetime(2026, 6, 11, 11, 30, 0)
    slots = [
        ("2026-06-11", 11),
        ("2026-06-11", 12),
        ("2026-06-11", 13),
    ]
    pv_by_key = {
        ("2026-06-11", 11): {"pv_kw": 2.0},
        ("2026-06-11", 12): {"pv_kw": 2.5},
        ("2026-06-11", 13): {"pv_kw": 1.0},
    }

    monkeypatch.setattr(pv_mod, "PV_CORRECTION_ENABLED", True)
    monkeypatch.setattr(
        pv_mod,
        "build_pv_intra_state",
        lambda _now, f50_current_kwh: {
            "enabled": True,
            "applied": True,
            "alpha": 0.5,
            "f50_current_kwh": f50_current_kwh,
            "a_so_far_kwh": 0.125,
            "telemetry_samples": 30,
            "f_elapsed_kwh": 1.0,
            "k_intra": 0.65,
            "reason": "ok",
        },
    )

    corrected, sources, meta = apply_pv_correction(slots, pv_by_key, now=now)
    assert corrected[("2026-06-11", 11)] == pytest.approx(0.775)
    assert corrected[("2026-06-11", 12)] == pytest.approx(1.625)
    assert corrected[("2026-06-11", 13)] == pytest.approx(1.0)
    assert sources[("2026-06-11", 11)] == "pv_intra_current"
    assert sources[("2026-06-11", 12)] == "pv_intra_next"
    assert sources[("2026-06-11", 13)] == "solcast_proxy"
    assert meta["applied"] is True


def test_pv_energy_so_far_from_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import planner.pv_correction as pv_mod

    monkeypatch.setattr(pv_mod, "TELEMETRY_DIR", tmp_path)
    day = "2026-06-11"
    path = tmp_path / f"telemetry_{day}.jsonl"
    rows = [
        {"local_hour": 11, "local_minute": 0, "pv_w": 240.0},
        {"local_hour": 11, "local_minute": 1, "pv_w": 260.0},
        {"local_hour": 11, "local_minute": 2, "pv_w": 250.0},
        {"local_hour": 12, "local_minute": 0, "pv_w": 500.0},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    now = datetime(2026, 6, 11, 11, 2, 30)
    got = pv_energy_so_far_in_hour(now)
    assert got is not None
    energy, samples = got
    assert samples == 3
    assert energy == pytest.approx((240 + 260 + 250) / 1000.0 / 60.0)
