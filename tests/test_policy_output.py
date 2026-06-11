"""Mapowanie HourPlan → policy i artefakt planner_output.json."""

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
    map_hour_to_policy,
    policy_label_pl,
    save_policy_artifact,
)


def _hp(
    *,
    net: float,
    bd: float,
    hour: int = 12,
) -> HourPlan:
    return HourPlan(
        date="2026-06-10",
        hour=hour,
        target_net_kwh=net,
        expected_cashflow_pln=0.0,
        soc_start_pct=50.0,
        soc_end_pct=55.0,
        battery_delta_kwh=bd,
    )


def _hin(*, pv: float = 5.0, load: float = 2.0) -> HourInputs:
    return HourInputs(
        date="2026-06-10",
        hour=12,
        load_kwh=load,
        pv_kwh=pv,
        import_pln_per_kwh=1.1,
        export_pln_per_kwh=0.4,
    )


@pytest.mark.parametrize(
    ("net", "bd", "expected", "allow_grid"),
    [
        (0.0, 0.0, "hold_neutral", False),
        (3.0, 0.0, "hold_export", False),
        (-2.0, 0.0, "hold_import", False),
        (0.0, 0.5, "charge", False),
        (-1.0, 0.5, "charge", True),
        (2.0, -0.8, "discharge_export", False),
        (0.0, -0.6, "discharge_serve", False),
    ],
)
def test_map_hour_to_policy_cases(
    net: float,
    bd: float,
    expected: str,
    allow_grid: bool,
) -> None:
    row = map_hour_to_policy(_hp(net=net, bd=bd), _hin())
    assert row.policy == expected
    assert row.params.allow_grid_charge is allow_grid
    assert row.params.pv_plan_kwh == pytest.approx(5.0)
    assert row.params.load_plan_kwh == pytest.approx(2.0)


def test_map_hour_eps_boundaries() -> None:
    eps_b = BATTERY_DELTA_EPS_KWH
    eps_n = NET_NEUTRAL_EPS_KWH
    assert map_hour_to_policy(_hp(net=eps_n + 0.01, bd=0.0)).policy == "hold_export"
    assert map_hour_to_policy(_hp(net=-eps_n - 0.01, bd=0.0)).policy == "hold_import"
    assert map_hour_to_policy(_hp(net=0.0, bd=eps_b + 0.01)).policy == "charge"
    assert map_hour_to_policy(_hp(net=0.0, bd=-eps_b - 0.01)).policy == "discharge_serve"


def test_policy_labels_pl() -> None:
    assert policy_label_pl("hold_export") == "eksport PV"
    assert policy_label_pl("discharge_serve") == "rozł.→dom"


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
    assert art.hours[0].policy == "hold_export"
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
    )
    raw = json.loads(out.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["hours"][0]["policy"] == "hold_export"
