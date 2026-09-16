"""Testy korekty load mid-hour (k_intra + pasma)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from planner.load_correction import (
    apply_load_correction,
    build_load_intra_meta,
    compute_load_k_intra,
    load_energy_so_far_in_hour,
    load_plan_current_hour_kwh,
    load_remainder_bands_kwh,
    mix_load_base_keep_ev,
)
from planner.hour_remainder import scale_hour_inputs_for_remainder
from planner.models import HourInputs


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


def test_load_remainder_bands_so_far_ate_p50_uses_recent_floor() -> None:
    """Regresja: A ≥ F50 + ciągniący dom → p50_rem z recent_kw, nie zero."""
    a = 3.14
    recent = 5.966
    alpha = 0.5
    p50, p25, p75 = load_remainder_bands_kwh(
        p50_full=3.14,
        p25_full=2.5,
        p75_full=4.0,
        a_so_far=a,
        alpha=alpha,
        recent_kw=recent,
    )
    expected = recent * (1.0 - alpha)
    assert p50 == pytest.approx(expected)
    assert p25 <= p50 <= p75
    assert p50 > 0.0
    assert p25 == pytest.approx(expected * 0.70)


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


def test_compute_load_k_intra_ok() -> None:
    k, reason = compute_load_k_intra(
        f50_kwh=3.14,
        a_so_far_kwh=3.14,
        alpha=0.5,
    )
    assert reason == "ok"
    assert k == pytest.approx(1.35)  # raw=2.0 clipped to K_MAX


def test_compute_load_k_intra_hour_start() -> None:
    k, reason = compute_load_k_intra(f50_kwh=3.0, a_so_far_kwh=0.0, alpha=0.0)
    assert k is None
    assert reason == "hour_start"


def test_load_plan_k_intra_only() -> None:
    plan, meta = load_plan_current_hour_kwh(
        f50_kwh=3.14,
        a_so_far_kwh=3.14,
        alpha=0.5,
        k_intra=1.35,
        rate_enabled=False,
    )
    # A + (1-α)·F50·k = 3.14 + 0.5*3.14*1.35
    assert plan == pytest.approx(3.14 + 0.5 * 3.14 * 1.35)
    assert meta["method"] == "k_intra"


def test_load_plan_rate_blend_raises_when_house_draws() -> None:
    """Wysokie tempo zużycia podnosi plan względem samego k_intra."""
    plan_k, meta_k = load_plan_current_hour_kwh(
        f50_kwh=3.14,
        a_so_far_kwh=3.14,
        alpha=0.5,
        k_intra=1.35,
        rate_enabled=False,
    )
    plan, meta = load_plan_current_hour_kwh(
        f50_kwh=3.14,
        a_so_far_kwh=3.14,
        alpha=0.5,
        k_intra=1.35,
        recent_kw=5.966,
        rate_enabled=True,
    )
    assert meta["method"] == "k_intra_rate_blend"
    assert meta["rate_blend_weight"] > 0.0
    assert plan > plan_k
    assert plan == pytest.approx(
        (1.0 - meta["rate_blend_weight"]) * plan_k
        + meta["rate_blend_weight"] * (3.14 + 5.966 * 0.5)
    )


def test_scale_hour_inputs_applies_load_plan_mid_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-hour: load_full nie zostaje na samym A gdy recent_kw wysokie."""
    monkeypatch.setattr(
        "planner.hour_remainder.net_kwh_so_far_for_hour",
        lambda _d, _h: 0.18,
    )
    now = datetime(2026, 7, 22, 11, 30, 0)
    hin = HourInputs(
        date="2026-07-22",
        hour=11,
        load_kwh=3.14,
        pv_kwh=6.7,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=0.14,
        load_kwh_p25=2.5,
        load_kwh_p75=4.0,
    )
    load_meta = {
        "enabled": True,
        "band_narrow_enabled": True,
        "applied": False,
        "alpha": 0.5,
        "a_so_far_kwh": 3.14,
        "telemetry_samples": 30,
        "recent_kw": 5.966,
        "recent_samples": 15,
        "k_intra": None,
        "reason": "pending",
        "plan_method": None,
        "load_plan_kwh": None,
        "rate_plan_kwh": None,
        "rate_blend_weight": None,
    }
    out = scale_hour_inputs_for_remainder(
        hin,
        now=now,
        pv_correction_meta={"a_so_far_kwh": None},
        load_meta=load_meta,
    )
    assert load_meta["applied"] is True
    assert load_meta["load_plan_kwh"] is not None
    assert out.load_so_far_kwh == pytest.approx(3.14)
    assert out.load_kwh > 3.14 + 1.0  # sensowna reszta, nie ~A
    rem = float(out.load_kwh) - float(out.load_so_far_kwh or 0.0)
    assert rem == pytest.approx(float(out.load_kwh) - 3.14)


def test_load_energy_so_far_in_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("planner.intra_hour.TELEMETRY_DIR", tmp_path)
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


def test_mix_load_base_keep_ev_does_not_scale_ev() -> None:
    """k na bazę domu; 3 kWh EV wchodzi 1:1 po mix."""
    base, total, p25, p75 = mix_load_base_keep_ev(
        load_base=2.0,
        ev_kwh=3.0,
        k=1.2,
        spill_min=30,
        load_p25=1.0,
        load_p75=2.5,
    )
    # w=0.5 → 0.5·2.4 + 0.5·2.0 = 2.2; EV +3
    assert base == pytest.approx(2.2)
    assert total == pytest.approx(5.2)
    assert p25 == pytest.approx(0.5 * 2.4 + 0.5 * 1.0)
    assert p75 == pytest.approx(5.2)  # EV podnosi p75 do total


def test_mix_load_base_keep_ev_full_spill_still_adds_ev() -> None:
    base, total, _p25, p75 = mix_load_base_keep_ev(
        load_base=2.0,
        ev_kwh=4.0,
        k=0.65,
        spill_min=60,
        load_p25=1.5,
        load_p75=2.5,
    )
    assert base == pytest.approx(1.3)
    assert total == pytest.approx(5.3)
    assert p75 == pytest.approx(5.3)


def test_load_k_carries_prev_hour_across_clock_roll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import planner.load_correction as load_mod

    monkeypatch.setattr("planner.intra_hour.TELEMETRY_DIR", tmp_path)
    monkeypatch.setattr(load_mod, "LOAD_CORRECTION_ENABLED", True)
    path = tmp_path / "telemetry_2026-06-11.jsonl"
    rows = [
        {"local_hour": 12, "local_minute": m, "consumption_w": 2000.0}
        for m in range(60)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    now = datetime(2026, 6, 11, 13, 0, 0)
    meta = build_load_intra_meta(now, f50_current_kwh=3.0, f50_prev_kwh=2.0)
    assert meta["reason"] == "prev_hour_load"
    assert meta["k_intra_source"] == "prev_hour"
    assert meta["k_intra"] == pytest.approx(1.0)
    assert meta["spill_min"] == pytest.approx(0.0)


def test_load_k_uses_current_after_carry_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import planner.load_correction as load_mod

    monkeypatch.setattr("planner.intra_hour.TELEMETRY_DIR", tmp_path)
    path = tmp_path / "telemetry_2026-06-11.jsonl"
    rows = [
        {"local_hour": 12, "local_minute": m, "consumption_w": 2000.0}
        for m in range(60)
    ] + [
        {"local_hour": 13, "local_minute": m, "consumption_w": 1000.0}
        for m in range(11)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    now = datetime(2026, 6, 11, 13, 10, 0)
    meta = build_load_intra_meta(now, f50_current_kwh=3.0, f50_prev_kwh=2.0)
    assert meta["k_intra_source"] == "current_hour"
    assert meta["reason"] == "ok"
    assert meta["k_intra"] == pytest.approx(float(meta["k_intra_current"]))
    assert meta["k_prev"] == pytest.approx(1.0)
    assert meta["spill_min"] == pytest.approx(10.0)


def test_apply_load_correction_mixes_next_hour_keeps_ev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import planner.load_correction as load_mod

    monkeypatch.setattr("planner.intra_hour.TELEMETRY_DIR", tmp_path)
    monkeypatch.setattr(load_mod, "LOAD_CORRECTION_ENABLED", True)
    path = tmp_path / "telemetry_2026-06-11.jsonl"
    rows = [
        {"local_hour": 12, "local_minute": m, "consumption_w": 2000.0}
        for m in range(30)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    now = datetime(2026, 6, 11, 12, 30, 0)
    load_by_key = {
        ("2026-06-11", 12): {
            "load_base_kwh_p50": 2.0,
            "load_kwh_p25": 1.5,
            "load_kwh_p75": 2.5,
            "source": "hist",
        },
        ("2026-06-11", 13): {
            "load_base_kwh_p50": 2.0,
            "load_kwh_p25": 1.0,
            "load_kwh_p75": 2.5,
            "source": "hist",
        },
    }
    ev = {("2026-06-11", 13): 3.0}
    corrected, sources, meta = apply_load_correction(
        [("2026-06-11", 12), ("2026-06-11", 13)],
        load_by_key,
        now=now,
        ev_schedule=ev,
    )
    assert sources[("2026-06-11", 13)] == "load_intra_next"
    nxt = corrected[("2026-06-11", 13)]
    base, total, p25, p75 = mix_load_base_keep_ev(
        load_base=2.0,
        ev_kwh=3.0,
        k=float(meta["k_intra"]),
        spill_min=float(meta["spill_min"]),
        load_p25=1.0,
        load_p75=2.5,
    )
    assert nxt["base"] == pytest.approx(base)
    assert nxt["total"] == pytest.approx(total)
    assert nxt["ev"] == pytest.approx(3.0)
    assert nxt["p25"] == pytest.approx(p25)
    assert nxt["p75"] == pytest.approx(p75)
    cur = corrected[("2026-06-11", 12)]
    assert sources[("2026-06-11", 12)] == "hist"
    assert cur["base"] == pytest.approx(2.0)
    assert cur["total"] == pytest.approx(2.0)
