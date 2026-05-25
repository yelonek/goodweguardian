"""Historia planów — niezmienne snapshoty i plan obowiązujący per godzina."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from planner.audit import append_audit, new_event, read_audit_events
from planner.config import PLANNER_AUDIT_DIR, PLANNER_PLANS_DIR, PLANNER_PLANS_HISTORY_DIR
from planner.config import ensure_planner_dirs
from planner.models import DailyPlan, HourPlan
from planner.plan_store import load_plan, load_plan_by_id, plan_effective_at, save_plan
import planner.audit as audit_mod
import planner.plan_store as ps_mod


def _minimal_plan(
    *,
    plan_id: str,
    local_date: str,
    generated_at: str,
    hour: int,
    target_net: float,
) -> DailyPlan:
    return DailyPlan(
        plan_id=plan_id,
        local_date=local_date,
        generated_at=generated_at,
        timezone="Europe/Warsaw",
        soc_start_pct=50.0,
        expected_total_cashflow_pln=1.0,
        optimizer="test",
        inputs_snapshot={},
        hours=[
            HourPlan(
                date=local_date,
                hour=hour,
                target_net_kwh=target_net,
                expected_cashflow_pln=0.5,
                soc_start_pct=50.0,
                soc_end_pct=55.0,
                battery_delta_kwh=0.0,
            )
        ],
    )


@pytest.fixture
def plan_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    plans = tmp_path / "plans"
    hist = plans / "history"
    hist.mkdir(parents=True)
    monkeypatch.setattr(ps_mod, "PLANNER_PLANS_DIR", plans)
    monkeypatch.setattr(ps_mod, "PLANNER_PLANS_HISTORY_DIR", hist)
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    monkeypatch.setattr(audit_mod, "PLANNER_AUDIT_DIR", audit_dir)
    ensure_planner_dirs()
    return plans


def test_history_keeps_both_plans(plan_dirs: Path) -> None:
    d = "2026-05-10"
    p1 = _minimal_plan(plan_id="aaa", local_date=d, generated_at="2026-05-09T20:00:00+00:00", hour=8, target_net=1.0)
    p2 = _minimal_plan(plan_id="bbb", local_date=d, generated_at="2026-05-10T06:00:00+00:00", hour=8, target_net=2.0)
    save_plan(p1)
    save_plan(p2)

    assert load_plan_by_id("aaa") is not None
    assert load_plan_by_id("bbb") is not None
    assert load_plan(d) is not None
    assert load_plan(d).plan_id == "bbb"
    assert load_plan_by_id("aaa").hours[0].target_net_kwh == pytest.approx(1.0)


def test_plan_effective_at_uses_audit_before_hour(plan_dirs: Path) -> None:
    d_iso = "2026-05-10"
    d = date(2026, 5, 10)
    evening = _minimal_plan(
        plan_id="eve",
        local_date=d_iso,
        generated_at="2026-05-09T20:00:00+00:00",
        hour=8,
        target_net=0.5,
    )
    morning = _minimal_plan(
        plan_id="morn",
        local_date=d_iso,
        generated_at="2026-05-10T06:00:00+00:00",
        hour=8,
        target_net=1.5,
    )
    save_plan(evening)
    append_audit(
        new_event(
            local_date=d_iso,
            kind="plan_created",
            plan_id="eve",
            payload={},
        ).model_copy(update={"ts_utc": "2026-05-09T20:00:00+00:00"})
    )
    save_plan(morning)
    append_audit(
        new_event(
            local_date=d_iso,
            kind="plan_created",
            plan_id="morn",
            payload={},
        ).model_copy(update={"ts_utc": "2026-05-10T06:00:00+00:00"})
    )

    # Godzina 8 w Europe/Warsaw — plan z rana (po 06 UTC), nie z wieczora
    eff = plan_effective_at(d, 8)
    assert eff is not None
    assert eff.plan_id == "morn"
    assert eff.hours[0].target_net_kwh == pytest.approx(1.5)
