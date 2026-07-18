"""Mapowanie HourPlan → exec_mode (względem wizji SOC) i artefakt planner_output.json."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from planner.models import DailyPlan, HourInputs, HourPlan, HourPolicyParams
from planner.policy_output import (
    SOC_GAP_EPS_PCT,
    build_policy_artifact,
    load_policy_artifact,
    map_hour_to_exec_mode,
    exec_mode_label_pl,
    save_policy_artifact,
)


def _hp(
    *,
    soc0: float = 50.0,
    soc_end: float = 50.0,
    net: float = 0.0,
    bd: float = 0.0,
    hour: int = 12,
    export_cashflow: float = 0.4,
) -> HourPlan:
    return HourPlan(
        date="2026-06-10",
        hour=hour,
        target_net_kwh=net,
        expected_cashflow_pln=export_cashflow,
        soc_start_pct=soc0,
        soc_end_pct=soc_end,
        battery_delta_kwh=bd,
    )


def _hin(
    *,
    pv: float = 5.0,
    load: float = 2.0,
    export_pln: float = 0.4,
    import_pln: float = 1.11,
) -> HourInputs:
    return HourInputs(
        date="2026-06-10",
        hour=12,
        load_kwh=load,
        pv_kwh=pv,
        import_pln_per_kwh=import_pln,
        export_pln_per_kwh=export_pln,
    )


def test_hold_soc_export_pv_surplus() -> None:
    """Spill PV tylko gdy bateria praktycznie pełna — inaczej soak."""
    row = map_hour_to_exec_mode(
        _hp(soc0=98.0, soc_end=98.0, net=3.0, bd=0.0),
        _hin(pv=5.0, load=2.0, export_pln=0.4),
    )
    assert row.exec_mode == "export_pv_surplus"


def test_hold_soc_with_headroom_soaks_not_exports() -> None:
    """Płaski SOC przy miejscu w baterii + nadwyżka PV → neutral (soak), nie eksport."""
    row = map_hour_to_exec_mode(
        _hp(soc0=50.0, soc_end=50.0, net=3.0, bd=0.0),
        _hin(pv=5.0, load=2.0, export_pln=0.4),
    )
    assert row.exec_mode == "neutral"


def test_tiny_soc_dip_with_pv_surplus_is_neutral_not_export_profit() -> None:
    """Szum trackingu ~1 pp w dół przy nadwyżce PV nie może włączać export_profit."""
    row = map_hour_to_exec_mode(
        _hp(soc0=15.0, soc_end=13.88, net=0.0, bd=-0.115),
        _hin(pv=1.78, load=1.54, export_pln=0.4, import_pln=0.59),
    )
    assert row.exec_mode == "neutral"


def test_soc_dump_with_zero_net_is_neutral_not_export_profit() -> None:
    """Spadek soc* do min przy net=0 (brak sprzedaży) + PV → soak, nie eksport zarobkowy."""
    row = map_hour_to_exec_mode(
        _hp(soc0=15.0, soc_end=10.5, net=0.0, bd=-0.47),
        _hin(pv=1.78, load=1.0, export_pln=0.09, import_pln=0.59),
    )
    assert row.exec_mode == "neutral"




def test_hold_soc_zero_rce_is_neutral() -> None:
    row = map_hour_to_exec_mode(
        _hp(soc0=50.0, soc_end=50.5, net=3.0, bd=0.0),
        _hin(export_pln=0.0),
    )
    assert row.exec_mode == "neutral"


def test_hold_soc_deficit_is_import_grid() -> None:
    row = map_hour_to_exec_mode(
        _hp(soc0=50.0, soc_end=50.0, net=-2.0, bd=0.0),
        _hin(pv=0.0, load=2.0),
    )
    assert row.exec_mode == "import_grid"


def test_rising_soc_with_pv_surplus_is_neutral_not_export() -> None:
    """soc0=10 → soc*=40 przy PV≫load → soak (neutral), nie export_pv_surplus."""
    row = map_hour_to_exec_mode(
        _hp(soc0=10.0, soc_end=40.0, net=2.8, bd=0.55),
        _hin(pv=4.5, load=1.0, export_pln=0.55),
        cheap_import_threshold_pln=0.61,
    )
    assert row.exec_mode == "neutral"
    assert row.params.allow_grid_charge is False


def test_rising_soc_cheap_import_without_pv_is_charge_grid() -> None:
    row = map_hour_to_exec_mode(
        _hp(soc0=20.0, soc_end=60.0, net=-4.5, bd=4.0),
        _hin(pv=0.0, load=0.5, import_pln=0.59, export_pln=0.1),
        cheap_import_threshold_pln=0.61,
    )
    assert row.exec_mode == "charge_grid"
    assert row.params.allow_grid_charge is True
    assert row.params.target_soc_pct == 60.0
    assert row.params.charge_pct is not None


def test_rising_soc_expensive_import_is_import_grid() -> None:
    """Luka SOC przy drogim imporcie → import_grid (PV DC), nie charge_grid."""
    row = map_hour_to_exec_mode(
        _hp(soc0=20.0, soc_end=60.0, net=-1.0, bd=0.5),
        _hin(pv=0.2, load=1.0, import_pln=1.11, export_pln=0.4),
        cheap_import_threshold_pln=0.61,
    )
    assert row.exec_mode == "import_grid"
    assert row.params.allow_grid_charge is False


def test_falling_soc_is_export_profit_with_floor() -> None:
    """soc0=80 → soc*=20 → export_profit z floor 20."""
    row = map_hour_to_exec_mode(
        _hp(soc0=80.0, soc_end=20.0, net=5.0, bd=-5.2),
        _hin(pv=0.1, load=0.5, export_pln=1.0),
    )
    assert row.exec_mode == "export_profit"
    assert row.params.soc_floor_pct == 20.0
    assert row.params.discharge_pct is not None


def test_export_profit_soc_floor_uses_end_not_start() -> None:
    """Pełna bateria na starcie h nie może ustawiać podłogi SOC na 100%."""
    row = map_hour_to_exec_mode(
        HourPlan(
            date="2026-06-18",
            hour=20,
            target_net_kwh=5.0,
            expected_cashflow_pln=8.0,
            soc_start_pct=100.0,
            soc_end_pct=43.0,
            battery_delta_kwh=-5.2,
        ),
        HourInputs(
            date="2026-06-18",
            hour=20,
            load_kwh=0.5,
            pv_kwh=0.1,
            import_pln_per_kwh=1.11,
            export_pln_per_kwh=1.69,
        ),
    )
    assert row.exec_mode == "export_profit"
    assert row.params.discharge_pct == 100
    assert row.params.soc_floor_pct == 43.0


def test_small_soc_drop_serve_load_stays_hold_path() -> None:
    """Mały spadek SOC (≤ eps) + deficyt PV → import_grid, nie export_profit."""
    assert SOC_GAP_EPS_PCT >= 1.0
    row = map_hour_to_exec_mode(
        _hp(soc0=15.0, soc_end=14.5, net=-0.194, bd=-0.05),
        _hin(pv=0.0, load=0.424, import_pln=0.59, export_pln=0.8),
    )
    assert row.exec_mode == "import_grid"


def test_discharge_h01_positive_net_is_export_profit() -> None:
    row = map_hour_to_exec_mode(
        HourPlan(
            date="2026-06-20",
            hour=1,
            target_net_kwh=0.068,
            expected_cashflow_pln=0.02,
            soc_start_pct=15.0,
            soc_end_pct=10.0,
            battery_delta_kwh=-0.46,
        ),
        HourInputs(
            date="2026-06-20",
            hour=1,
            load_kwh=0.392,
            pv_kwh=0.0,
            import_pln_per_kwh=0.59,
            export_pln_per_kwh=0.644,
        ),
    )
    assert row.exec_mode == "export_profit"
    assert row.params.discharge_pct is not None
    assert row.params.soc_floor_pct == 10.0


def test_exec_mode_labels_pl() -> None:
    assert exec_mode_label_pl("export_pv_surplus") == "eksport PV"
    assert exec_mode_label_pl("import_grid") == "import z sieci"


def test_build_and_save_policy_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "planner_output.json"
    monkeypatch.setattr("planner.policy_output.PLANNER_OUTPUT_PATH", out)

    plan = DailyPlan(
        plan_id="test-plan-id",
        local_date="2026-06-10",
        generated_at=datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC).isoformat(),
        timezone="Europe/Warsaw",
        soc_start_pct=98.0,
        soc_trajectory_pct=[98.0, 98.0],
        expected_total_cashflow_pln=1.0,
        optimizer="test",
        inputs_snapshot={},
        hours=[_hp(soc0=98.0, soc_end=98.0, net=3.0, bd=0.0)],
    )
    hin = [_hin()]
    art = build_policy_artifact(plan, hin, degraded=False, valid_minutes=10)
    assert art.plan_id == "test-plan-id"
    assert art.hours[0].exec_mode == "export_pv_surplus"
    assert art.valid_until > art.computed_at

    save_policy_artifact(art)
    loaded = load_policy_artifact()
    assert loaded is not None
    assert loaded.hours[0].params == HourPolicyParams(
        target_net_kwh=3.0,
        battery_delta_kwh=0.0,
        soc_end_pct=98.0,
        pv_plan_kwh=5.0,
        load_plan_kwh=2.0,
        allow_grid_charge=False,
        discharge_pct=None,
        charge_pct=None,
        soc_floor_pct=None,
        target_soc_pct=None,
    )
    raw = json.loads(out.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 2
    assert raw["hours"][0]["exec_mode"] == "export_pv_surplus"
