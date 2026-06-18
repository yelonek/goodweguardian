"""Mapowanie HourPlan → exec_mode i artefakt planner_output.json."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from planner.config import PLANNER_OUTPUT_PATH
from planner.models import DailyPlan, HourInputs, HourPlan, HourPolicyParams
from planner.policy_output import (
    BATTERY_DELTA_EPS_KWH,
    NET_NEUTRAL_EPS_KWH,
    build_policy_artifact,
    load_policy_artifact,
    map_hour_to_exec_mode,
    exec_mode_label_pl,
    save_policy_artifact,
)


def _hp(
    *,
    net: float,
    bd: float,
    hour: int = 12,
    export_cashflow: float = 0.4,
) -> HourPlan:
    return HourPlan(
        date="2026-06-10",
        hour=hour,
        target_net_kwh=net,
        expected_cashflow_pln=export_cashflow,
        soc_start_pct=50.0,
        soc_end_pct=55.0,
        battery_delta_kwh=bd,
    )


def _hin(*, pv: float = 5.0, load: float = 2.0, export_pln: float = 0.4) -> HourInputs:
    return HourInputs(
        date="2026-06-10",
        hour=12,
        load_kwh=load,
        pv_kwh=pv,
        import_pln_per_kwh=1.1,
        export_pln_per_kwh=export_pln,
    )


@pytest.mark.parametrize(
    ("net", "bd", "export_pln", "expected"),
    [
        (0.0, 0.0, 0.4, "neutral"),
        (3.0, 0.0, 0.4, "export_pv_surplus"),
        (3.0, 0.0, 0.0, "neutral"),
        (-2.0, 0.0, 0.4, "import_grid"),
        (0.0, 0.5, 0.4, "neutral"),
        (-1.0, 0.5, 0.4, "charge_grid"),
        (2.0, -0.8, 0.5, "export_profit"),
        (0.0, -0.6, 0.4, "neutral"),
        (-0.4, -0.1, 0.4, "import_grid"),
    ],
)
def test_map_hour_to_exec_mode_cases(
    net: float,
    bd: float,
    export_pln: float,
    expected: str,
) -> None:
    row = map_hour_to_exec_mode(_hp(net=net, bd=bd), _hin(export_pln=export_pln))
    assert row.exec_mode == expected


def test_map_hour_eps_boundaries() -> None:
    eps_b = BATTERY_DELTA_EPS_KWH
    eps_n = NET_NEUTRAL_EPS_KWH
    assert (
        map_hour_to_exec_mode(_hp(net=eps_n + 0.01, bd=0.0), _hin(export_pln=0.4)).exec_mode
        == "export_pv_surplus"
    )
    assert map_hour_to_exec_mode(_hp(net=eps_n + 0.01, bd=0.0), _hin(export_pln=0.0)).exec_mode == "neutral"
    assert map_hour_to_exec_mode(_hp(net=-eps_n - 0.01, bd=0.0)).exec_mode == "import_grid"
    assert map_hour_to_exec_mode(_hp(net=0.0, bd=eps_b + 0.01)).exec_mode == "neutral"
    assert map_hour_to_exec_mode(_hp(net=0.0, bd=-eps_b - 0.01)).exec_mode == "neutral"


def test_exec_mode_labels_pl() -> None:
    assert exec_mode_label_pl("export_pv_surplus") == "eksport PV"
    assert exec_mode_label_pl("import_grid") == "import z sieci"


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


def test_build_and_save_policy_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "planner_output.json"
    monkeypatch.setattr("planner.policy_output.PLANNER_OUTPUT_PATH", out)

    plan = DailyPlan(
        plan_id="test-plan-id",
        local_date="2026-06-10",
        generated_at=datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC).isoformat(),
        timezone="Europe/Warsaw",
        soc_start_pct=50.0,
        expected_total_cashflow_pln=1.0,
        optimizer="test",
        inputs_snapshot={},
        hours=[_hp(net=3.0, bd=0.0)],
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
        soc_end_pct=55.0,
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
