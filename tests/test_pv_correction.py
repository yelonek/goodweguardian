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
    pv_recent_average_kw,
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
    plan, meta = pv_plan_current_hour_kwh(
        f50_kwh=2.0,
        a_so_far_kwh=0.125,
        alpha=0.5,
        k_intra=0.65,
        rate_enabled=False,
    )
    assert plan == pytest.approx(0.775)
    assert meta["method"] == "k_intra"
    assert pv_plan_next_hour_kwh(f50_kwh=2.0, k_intra=0.65) == pytest.approx(1.3)


def test_pv_plan_rate_blend_lowers_cloudy_hour() -> None:
    """Chmury: niska recent_kw obcina prognozę względem samego k_intra."""
    plan, meta = pv_plan_current_hour_kwh(
        f50_kwh=4.44,
        a_so_far_kwh=1.0,
        alpha=0.5,
        k_intra=0.65,
        recent_kw=1.0,
        rate_enabled=True,
    )
    k_only = 1.0 + 0.5 * 4.44 * 0.65
    rate_only = 1.0 + 1.0 * 0.5
    assert meta["method"] == "k_intra_rate_blend"
    assert plan < k_only
    assert plan == pytest.approx(0.4 * k_only + 0.6 * rate_only, rel=1e-4)
    assert plan == pytest.approx(1.876, rel=1e-3)


def test_pv_recent_average_kw_from_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import planner.pv_correction as pv_mod

    monkeypatch.setattr(pv_mod, "TELEMETRY_DIR", tmp_path)
    path = tmp_path / "telemetry_2026-06-11.jsonl"
    rows = [
        {"local_hour": 12, "local_minute": 10, "pv_w": 1000.0},
        {"local_hour": 12, "local_minute": 20, "pv_w": 2000.0},
        {"local_hour": 12, "local_minute": 25, "pv_w": 3000.0},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    now = datetime(2026, 6, 11, 12, 25, 0)
    got = pv_mod.pv_recent_average_kw(now, window_min=15)
    assert got is not None
    avg, n = got
    assert n == 3
    assert avg == pytest.approx(2.0)


def test_compute_k_intra_example_1130(monkeypatch: pytest.MonkeyPatch) -> None:
    import planner.pv_correction as pv_mod

    monkeypatch.setattr(pv_mod, "PV_CORRECTION_DYNAMIC_CLIP_ENABLED", False)
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


def test_compute_k_intra_dynamic_clip_wider_late_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pod koniec godziny clip pozwala zejść niżej niż 0.65."""
    import planner.pv_correction as pv_mod

    monkeypatch.setattr(pv_mod, "PV_CORRECTION_DYNAMIC_CLIP_ENABLED", True)
    _, _, detail = pv_mod.compute_k_intra_detail(
        f50_kwh=4.44,
        a_so_far_kwh=0.5,
        alpha=0.5,
        k_min=0.65,
        k_max=1.35,
    )
    assert detail["dynamic_clip_weight"] > 0.8
    assert detail["clip_min_effective"] < 0.65
    assert detail["k_raw"] == pytest.approx(0.5 / (0.5 * 4.44), rel=1e-4)
    assert detail["k_intra"] == pytest.approx(max(detail["k_raw"], detail["clip_min_effective"]), rel=1e-4)
    assert detail["k_intra"] < 0.65

    monkeypatch.setattr(pv_mod, "PV_CORRECTION_DYNAMIC_CLIP_ENABLED", False)
    k_fixed, _ = compute_k_intra(
        f50_kwh=4.44,
        a_so_far_kwh=0.5,
        alpha=0.5,
        k_min=0.65,
        k_max=1.35,
    )
    assert k_fixed == pytest.approx(0.65)


def test_effective_clip_bounds_early_vs_late() -> None:
    import planner.pv_correction as pv_mod

    early = pv_mod.effective_clip_bounds(0.1)
    late = pv_mod.effective_clip_bounds(0.6)
    assert early[0] == pytest.approx(0.65)
    assert early[1] == pytest.approx(1.35)
    assert late[0] < 0.65
    assert late[1] > 1.35


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
            "recent_kw": None,
            "recent_samples": 0,
            "f_elapsed_kwh": 1.0,
            "k_intra": 0.65,
            "reason": "ok",
            "plan_method": None,
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
