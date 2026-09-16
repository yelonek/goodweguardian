"""Testy planera — optymalizator, audyt, economics."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from economics import cashflow_pln_for_hour
from planner.battery import BatteryParams, battery_delta_from_net
from planner.hour_remainder import (
    green_export_cap_rhs_kwh,
    hour_remaining_fraction,
    meter_export_so_far_kwh,
    pv_remainder_kwh,
    scale_hour_inputs_for_remainder,
)
from planner.models import HourInputs
from planner.optimizer import optimize_horizon
from planner.audit import append_audit, new_event, read_audit_events
from planner.config import ensure_planner_dirs
import planner.audit as audit_mod
import planner.optimizer as opt_mod


def test_battery_delta_sign() -> None:
    # PV 2, load 1, net export 0.5 -> battery +0.5
    bd = battery_delta_from_net(pv_kwh=2.0, load_kwh=1.0, net_kwh=0.5)
    assert bd == pytest.approx(0.5)


def _soc_in_bounds(soc_pct: float, bp: BatteryParams) -> bool:
    return bp.soc_min_pct - 0.1 <= soc_pct <= bp.soc_max_pct + 0.1


def test_lp_no_spurious_export_at_low_rce_when_storing_pays() -> None:
    """Przy niskim RCE i nadwyżce PV: trzymaj energię na późniejszy eksport, nie +0,25 z siatki."""
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-06-09",
            hour=12,
            load_kwh=1.96,
            pv_kwh=2.61,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.25,
        ),
        HourInputs(
            date="2026-06-09",
            hour=20,
            load_kwh=0.55,
            pv_kwh=0.16,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.95,
        ),
    ]
    res = optimize_horizon(hours, soc_start_pct=45.0, params=bp)
    assert res.hours[0].target_net_kwh == pytest.approx(0.0, abs=0.05)
    assert res.hours[1].target_net_kwh > 0.5


def test_lp_energy_balance_and_soc_limits() -> None:
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-06-09",
            hour=14,
            load_kwh=3.6,
            pv_kwh=2.02,
            import_pln_per_kwh=0.59,
            export_pln_per_kwh=0.42,
        ),
        HourInputs(
            date="2026-06-09",
            hour=20,
            load_kwh=0.55,
            pv_kwh=0.16,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.72,
        ),
    ]
    res = optimize_horizon(hours, soc_start_pct=55.0, params=bp)
    for hp in res.hours:
        assert _soc_in_bounds(hp.soc_start_pct, bp)
        assert _soc_in_bounds(hp.soc_end_pct, bp)
    assert res.total_cashflow_pln > 0.0


def test_battery_wear_reduces_export_vs_no_wear(monkeypatch: pytest.MonkeyPatch) -> None:
    import planner.optimizer as opt_mod

    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-06-09",
            hour=15,
            load_kwh=0.5,
            pv_kwh=3.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.35,
        ),
    ]
    monkeypatch.setattr(opt_mod, "PLANNER_BATTERY_CYCLE_COST_PLN", 0.0)
    no_wear = optimize_horizon(hours, soc_start_pct=80.0, params=bp)
    monkeypatch.setattr(opt_mod, "PLANNER_BATTERY_CYCLE_COST_PLN", 0.10)
    with_wear = optimize_horizon(hours, soc_start_pct=80.0, params=bp)
    assert with_wear.hours[0].target_net_kwh <= no_wear.hours[0].target_net_kwh + 1e-6
    if with_wear.hours[0].battery_wear_cost_pln > 0:
        grid = cashflow_pln_for_hour(
            with_wear.hours[0].target_net_kwh,
            rce_pln_per_kwh=0.35,
            import_pln_per_kwh=1.11,
        )
        assert with_wear.hours[0].expected_cashflow_pln == pytest.approx(
            grid - with_wear.hours[0].battery_wear_cost_pln
        )


def test_optimizer_prefers_export_when_rce_high() -> None:
    """Przy dużym RCE i nadwyżce PV planer powinien eksportować zamiast trzymać w magazynie."""
    bp = BatteryParams(capacity_kwh=5.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    hours = [
        HourInputs(
            date="2026-05-01",
            hour=12,
            load_kwh=0.5,
            pv_kwh=4.0,
            import_pln_per_kwh=0.8,
            export_pln_per_kwh=2.0,
        ),
    ]
    res = optimize_horizon(hours, soc_start_pct=50.0, params=bp)
    hp = res.hours[0]
    assert hp.target_net_kwh >= 0.0
    grid_cf = cashflow_pln_for_hour(
        hp.target_net_kwh,
        rce_pln_per_kwh=2.0,
        import_pln_per_kwh=0.8,
    )
    assert hp.expected_cashflow_pln == pytest.approx(grid_cf - hp.battery_wear_cost_pln)
    assert res.total_cashflow_pln > 0.0


def test_audit_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_mod, "PLANNER_AUDIT_DIR", tmp_path)
    ensure_planner_dirs()
    ev = new_event(local_date="2026-05-01", kind="plan_created", plan_id="p1", payload={"x": 1})
    append_audit(ev)
    got = read_audit_events("2026-05-01")
    assert len(got) == 1
    assert got[0].kind == "plan_created"
    assert got[0].payload["x"] == 1


def test_hour_remaining_fraction_at_fifty_minutes() -> None:
    now = datetime(2026, 6, 14, 20, 50, 0)
    frac = hour_remaining_fraction(now, date="2026-06-14", hour=20)
    assert frac == pytest.approx(10 / 60, rel=0.01)
    assert hour_remaining_fraction(now, date="2026-06-14", hour=21) == 1.0


def test_scale_hour_inputs_for_remainder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "planner.hour_remainder.net_kwh_so_far_for_hour",
        lambda _d, _h: None,
    )
    now = datetime(2026, 6, 14, 20, 50, 0)
    hin = HourInputs(
        date="2026-06-14",
        hour=20,
        load_kwh=0.6,
        pv_kwh=0.12,
        pv_kwh_p10=0.05,
        pv_kwh_p90=0.2,
        load_kwh_p75=0.7,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=1.69,
    )
    scaled = scale_hour_inputs_for_remainder(
        hin,
        now=now,
        pv_correction_meta={"a_so_far_kwh": 0.02},
        load_meta={"a_so_far_kwh": 0.5, "alpha": 50 / 60, "band_narrow_enabled": True},
    )
    frac = 10 / 60
    assert scaled.hour_fraction == pytest.approx(frac, rel=0.01)
    # Pełna godzina = so_far + reszta (nie sama reszta).
    assert scaled.pv_so_far_kwh == pytest.approx(0.02)
    assert scaled.load_so_far_kwh == pytest.approx(0.5)
    assert scaled.pv_kwh == pytest.approx(0.02 + 0.10)
    assert scaled.load_kwh >= 0.5
    assert scaled.pv_kwh_p10 is not None
    assert scaled.pv_kwh_p10 >= 0.02


def test_scale_hour_inputs_h11_pessimistic_remainder_has_surplus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regresja: w słoneczny mid-hour p10 reszty PV nie zeruje się (A > p10_full)."""
    monkeypatch.setattr(
        "planner.hour_remainder.net_kwh_so_far_for_hour",
        lambda _d, _h: 0.0,
    )
    now = datetime(2026, 7, 16, 11, 40, 0)
    hin = HourInputs(
        date="2026-07-16",
        hour=11,
        load_kwh=4.0,
        pv_kwh=5.3,
        pv_kwh_p10=2.4,
        pv_kwh_p90=5.5,
        load_kwh_p75=4.5,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=0.515,
    )
    scaled = scale_hour_inputs_for_remainder(
        hin,
        now=now,
        pv_correction_meta={
            "a_so_far_kwh": 3.5,
            "recent_kw": 4.5,
        },
        load_meta={
            "a_so_far_kwh": 2.5,
            "alpha": 40 / 60,
            "recent_kw": 3.0,
            "band_narrow_enabled": True,
        },
    )
    frac = 20 / 60
    assert scaled.hour_fraction == pytest.approx(frac, rel=0.01)
    assert scaled.pv_kwh_p10 is not None
    pv_p10_rem = scaled.pv_kwh_p10 - float(scaled.pv_so_far_kwh or 0.0)
    assert pv_p10_rem > 1.0
    # Naiwne max(0, 2.4−3.5)=0 — narrowing musi dać > 0.
    assert pv_p10_rem > 0.0


def test_scale_hour_inputs_load_narrow_not_naive_frac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Przy load_so_far pesymistyczny p75 rem ≠ ślepe full×frac."""
    monkeypatch.setattr(
        "planner.hour_remainder.net_kwh_so_far_for_hour",
        lambda _d, _h: None,
    )
    now = datetime(2026, 7, 16, 11, 40, 0)
    hin = HourInputs(
        date="2026-07-16",
        hour=11,
        load_kwh=4.0,
        pv_kwh=5.0,
        load_kwh_p25=2.0,
        load_kwh_p75=6.0,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=0.5,
    )
    scaled = scale_hour_inputs_for_remainder(
        hin,
        now=now,
        pv_correction_meta={"a_so_far_kwh": 2.0},
        load_meta={
            "a_so_far_kwh": 3.0,
            "alpha": 40 / 60,
            "recent_kw": 4.0,
            "band_narrow_enabled": True,
        },
    )
    frac = 20 / 60
    naive_p75_rem = 6.0 * frac
    p75_rem = scaled.load_kwh_p75 - 3.0
    assert p75_rem != pytest.approx(naive_p75_rem)
    assert p75_rem > 0.0
    assert scaled.load_kwh_p75 >= scaled.load_kwh


def test_green_export_cap_rhs_is_remainder_pv_plus_meter_export() -> None:
    mid = HourInputs(
        date="2026-08-29",
        hour=20,
        load_kwh=0.61,
        pv_kwh=1.0,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=1.09,
        hour_fraction=0.5,
        net_so_far_kwh=2.15,
        pv_so_far_kwh=0.4,
        load_so_far_kwh=0.3,
    )
    assert pv_remainder_kwh(mid) == pytest.approx(0.6)
    assert meter_export_so_far_kwh(mid) == pytest.approx(2.15)
    assert green_export_cap_rhs_kwh(mid) == pytest.approx(2.75)
    assert meter_export_so_far_kwh(
        HourInputs(
            date="2026-08-29",
            hour=20,
            load_kwh=0.6,
            pv_kwh=0.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.09,
            net_so_far_kwh=-0.4,
        )
    ) == pytest.approx(0.0)
    full = HourInputs(
        date="2026-08-29",
        hour=21,
        load_kwh=0.57,
        pv_kwh=2.0,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=0.88,
    )
    assert green_export_cap_rhs_kwh(full) == pytest.approx(2.0)


def test_partial_current_hour_limits_soc_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regresja 20:50: nie planuj końca h20 na ~10% SOC przy starcie ~59%."""
    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    frac = 10 / 60
    hours = [
        HourInputs(
            date="2026-06-14",
            hour=20,
            load_kwh=0.5 * frac,
            pv_kwh=0.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.69,
            hour_fraction=frac,
        ),
        HourInputs(
            date="2026-06-14",
            hour=21,
            load_kwh=0.5,
            pv_kwh=0.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.69,
        ),
    ]
    res = optimize_horizon(hours, soc_start_pct=59.0, params=bp)
    # max ~0.83 kWh discharge w reszcie h20 → SOC nie spada o ~50 pp jak przy pełnej h
    assert res.hours[0].soc_end_pct > 45.0
    assert res.hours[1].target_net_kwh > 0.3


def test_mid_hour_green_cap_keeps_already_exported_plus_remainder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regresja 2026-08-29 20:30: N₀=+2.15, 30 min, RCE h20>h21 → nie tnij exp do pmax.

    Stary cap ``exp ≤ dis_g ≤ 2.75`` zjadał już sprzedane kWh. Ma być
    ``exp ≤ N₀ + PV_rem + dis_g`` ≈ 2.15 + 2.75.
    """
    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    bp = BatteryParams(
        capacity_kwh=10.77,
        soc_min_pct=11.0,
        soc_max_pct=100.0,
        max_power_kwh_per_h=5.5,
    )
    hours = [
        HourInputs(
            date="2026-08-29",
            hour=20,
            load_kwh=0.61,
            pv_kwh=0.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.09,
            hour_fraction=0.5,
            net_so_far_kwh=2.15,
            load_so_far_kwh=0.30,
            pv_so_far_kwh=0.0,
        ),
        HourInputs(
            date="2026-08-29",
            hour=21,
            load_kwh=0.57,
            pv_kwh=0.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.88,
        ),
    ]
    res = optimize_horizon(hours, soc_start_pct=57.0, params=bp, green_stock_kwh=6.14)
    # Stary bug: target_net ≈ 2.6 (sufit pmax). Poprawnie: dociągnij zrzut w droższej h20.
    assert res.hours[0].target_net_kwh > 3.5
    assert res.hours[0].target_net_kwh > res.hours[1].target_net_kwh


def test_partial_hour_keeps_more_soc_than_full_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    bp = BatteryParams(capacity_kwh=10.0, soc_min_pct=10.0, soc_max_pct=100.0, max_power_kwh_per_h=5.0)
    base = dict(
        date="2026-06-14",
        hour=20,
        load_kwh=0.5,
        pv_kwh=0.0,
        import_pln_per_kwh=1.11,
        export_pln_per_kwh=1.69,
    )
    full = optimize_horizon(
        [HourInputs(**base, hour_fraction=1.0)],
        soc_start_pct=59.0,
        params=bp,
    )
    partial = optimize_horizon(
        [
            HourInputs(
                date=base["date"],
                hour=base["hour"],
                load_kwh=0.5 * (10 / 60),
                pv_kwh=base["pv_kwh"],
                import_pln_per_kwh=base["import_pln_per_kwh"],
                export_pln_per_kwh=base["export_pln_per_kwh"],
                hour_fraction=10 / 60,
            )
        ],
        soc_start_pct=59.0,
        params=bp,
    )
    assert partial.hours[0].soc_end_pct > full.hours[0].soc_end_pct


def test_milp_2026_07_29_pv_surplus_export_with_grid_charge_arbitrage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regresja 2026-07-29: scenariusz PV-surplus rano + zakup z sieci w południe.

    Kontekst: h09 PV nadwyżka 1.87 kWh (eksport 0.556 PLN/kWh),
    h13 deficyt 0.77 kWh (import G12 noc 0.59 PLN/kWh).

    Pozorna "strata": sprzedajemy za 0.556 i kupujemy za 0.590.
    W rzeczywistości: eksportujemy 1.87 kWh i importujemy tylko 0.77 kWh —
    inne wolumeny, nie ta sama energia w kółko.
    Cashflow A (eksport+import) = +0.585 PLN > 0 (brak transakcji).

    Przy wysokim wieczornym RCE (h19-20 @ 1.31-1.38 PLN/kWh) MILP zatrzymuje
    energię w baterii rano (nie eksportuje) i kupuje ekstra z sieci o 13–14
    żeby mieć SOC na wieczorny eksport.
    """
    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    bp = BatteryParams(
        capacity_kwh=10.77,
        soc_min_pct=11.0,
        soc_max_pct=100.0,
        max_power_kwh_per_h=5.0,
    )

    # Dane z realnego planu 2026-07-29 (load_plan_kwh z planner_output — zawiera EV + korekcję)
    hours = [
        HourInputs(date="2026-07-29", hour=9,  load_kwh=4.19, pv_kwh=6.06, import_pln_per_kwh=1.11, export_pln_per_kwh=0.556),
        HourInputs(date="2026-07-29", hour=10, load_kwh=3.44, pv_kwh=7.37, import_pln_per_kwh=1.11, export_pln_per_kwh=0.14),
        HourInputs(date="2026-07-29", hour=11, load_kwh=3.39, pv_kwh=2.35, import_pln_per_kwh=1.11, export_pln_per_kwh=0.05),
        HourInputs(date="2026-07-29", hour=12, load_kwh=3.52, pv_kwh=2.33, import_pln_per_kwh=1.11, export_pln_per_kwh=0.05),
        # h13: G12 noc — import 0.59, eksport 0.556 (RCE niskie w południe)
        HourInputs(date="2026-07-29", hour=13, load_kwh=2.87, pv_kwh=2.10, import_pln_per_kwh=0.59, export_pln_per_kwh=0.556),
        HourInputs(date="2026-07-29", hour=14, load_kwh=1.19, pv_kwh=1.98, import_pln_per_kwh=0.59, export_pln_per_kwh=0.556),
        HourInputs(date="2026-07-29", hour=15, load_kwh=0.73, pv_kwh=1.74, import_pln_per_kwh=1.11, export_pln_per_kwh=0.556),
        HourInputs(date="2026-07-29", hour=16, load_kwh=0.67, pv_kwh=2.79, import_pln_per_kwh=1.11, export_pln_per_kwh=0.556),
        HourInputs(date="2026-07-29", hour=17, load_kwh=0.51, pv_kwh=1.87, import_pln_per_kwh=1.11, export_pln_per_kwh=0.690),
        HourInputs(date="2026-07-29", hour=18, load_kwh=0.40, pv_kwh=0.83, import_pln_per_kwh=1.11, export_pln_per_kwh=0.911),
        # h19-h20: wysoki RCE — arbitraż 0.59 → 1.31-1.38 PLN/kWh
        HourInputs(date="2026-07-29", hour=19, load_kwh=0.49, pv_kwh=0.37, import_pln_per_kwh=1.11, export_pln_per_kwh=1.313),
        HourInputs(date="2026-07-29", hour=20, load_kwh=0.62, pv_kwh=0.13, import_pln_per_kwh=1.11, export_pln_per_kwh=1.377),
    ]

    res = optimize_horizon(hours, soc_start_pct=14.0, params=bp)

    h13 = res.hours[4]
    h14 = res.hours[5]
    h19 = res.hours[10]
    h20 = res.hours[11]

    # Wieczorny eksport musi wystąpić (arbitraż jest bardzo opłacalny)
    assert h19.target_net_kwh > 1.0 or h20.target_net_kwh > 1.0, (
        "Przy RCE h19=1.31 PLN/kWh i h20=1.38 PLN/kWh planer powinien eksportować wieczorem"
    )

    # Import z sieci o 13–14 jest opłacalny: kupuje za 0.59 → sprzedaje za 1.38 (+134%)
    grid_import_13_14 = max(0.0, -h13.target_net_kwh) + max(0.0, -h14.target_net_kwh)
    assert grid_import_13_14 > 0.5, (
        f"Przy arbitrażu G12-noc (0.59) → wieczór (1.38 PLN/kWh) planer powinien "
        f"kupować o 13–14, a kupił tylko {grid_import_13_14:.2f} kWh"
    )

    # Cashflow z baterią naładowaną rano z PV (h09-10) + zakup o 13–14 +
    # eksploracja wieczorna musi być wyraźnie > 0
    assert res.total_cashflow_pln > 5.0, (
        f"Oczekiwany cashflow >5 PLN dla tego arbitrażu, otrzymano {res.total_cashflow_pln:.2f} PLN"
    )

    # Przy drogim wieczorze (1.38 PLN/kWh) planer nie eksportuje rano za grosze (0.556),
    # tylko ładuje baterię z PV i kupuje ekstra z sieci o 13–14 (0.59 PLN/kWh).
    # Net h09 musi być ≤ 0 (ładowanie, nie eksport do sieci)
    assert res.hours[0].target_net_kwh <= 0.1, (
        f"h09: przy RCE wieczornym 1.38 PLN/kWh MILP nie powinien eksportować rano za 0.556, "
        f"a eksportuje {res.hours[0].target_net_kwh:.3f} kWh"
    )


def test_milp_feasible_when_soc_below_configured_min(monkeypatch) -> None:
    """SOC 10% przy planner_soc_min=11% nie może robić MILP infeasible."""
    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    bp = BatteryParams(
        capacity_kwh=10.77,
        soc_min_pct=11.0,
        soc_max_pct=100.0,
        max_power_kwh_per_h=5.0,
    )
    hours = [
        HourInputs(
            date="2026-07-31",
            hour=h,
            load_kwh=0.5,
            pv_kwh=0.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.2,
        )
        for h in range(6, 12)
    ]
    res = optimize_horizon(hours, soc_start_pct=10.0, params=bp)
    assert res.hours, "oczekiwany plan, nie pusty fallback"
    assert res.soc_trajectory_pct[0] == pytest.approx(10.0)
    # Bez PV / taniej taryfy / drogiego eksportu: nie forsować dokładowania do 11%.
    assert all(h.soc_end_pct <= 10.5 for h in res.hours)


def test_no_forced_grid_charge_to_min_on_expensive_tariff(monkeypatch) -> None:
    """Poniżej flooru: czekaj na tanią taryfę, nie ładuj z drogiego importu „do 11%”."""
    monkeypatch.setattr(opt_mod, "planner_scenario_optimizer_enabled", lambda: False)
    bp = BatteryParams(
        capacity_kwh=10.77,
        soc_min_pct=11.0,
        soc_max_pct=100.0,
        max_power_kwh_per_h=5.0,
    )
    hours = [
        # Droga strefa dzienna — nie dokładowywać z sieci do min.
        HourInputs(
            date="2026-07-31",
            hour=10,
            load_kwh=0.4,
            pv_kwh=0.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.1,
        ),
        HourInputs(
            date="2026-07-31",
            hour=11,
            load_kwh=0.4,
            pv_kwh=0.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=0.1,
        ),
        # Tania G12 noc — ewentualne ładowanie dopiero tu (jeśli w ogóle opłacalne).
        HourInputs(
            date="2026-07-31",
            hour=13,
            load_kwh=0.4,
            pv_kwh=0.0,
            import_pln_per_kwh=0.59,
            export_pln_per_kwh=0.1,
        ),
        HourInputs(
            date="2026-07-31",
            hour=14,
            load_kwh=0.4,
            pv_kwh=0.0,
            import_pln_per_kwh=0.59,
            export_pln_per_kwh=0.1,
        ),
    ]
    res = optimize_horizon(hours, soc_start_pct=10.0, params=bp)
    assert res.hours
    expensive = res.hours[:2]
    for h in expensive:
        assert h.battery_delta_kwh <= 0.05, (
            f"h{h.hour}: nie ładuj z drogiego importu tylko żeby wrócić na 11% "
            f"(battery_delta={h.battery_delta_kwh:.3f})"
        )
        assert h.soc_end_pct < 11.0


def test_fallback_neutral_holds_meter_so_far_not_clawback() -> None:
    """Regresja 2026-08-29 19:40: MILP infeasible + N₀=+2.22 → nie planuj importu 2 kWh."""
    from planner.optimizer import _fallback_neutral

    bp = BatteryParams(
        capacity_kwh=10.77, soc_min_pct=11.0, soc_max_pct=100.0, max_power_kwh_per_h=5.5
    )
    hours = [
        HourInputs(
            date="2026-08-29",
            hour=19,
            load_kwh=0.8,
            pv_kwh=0.05,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.31,
            hour_fraction=20 / 60,
            net_so_far_kwh=2.22,
            load_so_far_kwh=0.5,
            pv_so_far_kwh=0.04,
        ),
        HourInputs(
            date="2026-08-29",
            hour=20,
            load_kwh=0.6,
            pv_kwh=0.0,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.38,
        ),
    ]
    res = _fallback_neutral(hours, 66.0, bp)
    assert res.scenario_meta is not None
    assert res.scenario_meta.get("fallback") == "neutral_hold_meter"
    assert res.hours[0].target_net_kwh == pytest.approx(2.22)
    assert res.hours[1].target_net_kwh == pytest.approx(0.0)
    # Δ baterii = PV_rem − load_rem (dom z magazynu), nie +N₀ z sieci.
    assert res.hours[0].battery_delta_kwh < 0.0

