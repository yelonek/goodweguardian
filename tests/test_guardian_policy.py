"""Odczyt aktywnego wiersza policy."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from planner.models import DailyPlan, HourInputs, HourPlan
from planner.policy_output import build_policy_artifact, save_policy_artifact


def test_active_policy_row_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import planner.policy_output as po_mod
    from planner.guardian_policy import active_policy_row

    out = tmp_path / "planner_output.json"
    monkeypatch.setattr(po_mod, "PLANNER_OUTPUT_PATH", out)

    now = datetime(2026, 6, 10, 12, 30, 0)
    plan = DailyPlan(
        plan_id="p1",
        local_date="2026-06-10",
        generated_at=datetime(2026, 6, 10, 12, 25, 0, tzinfo=UTC).isoformat(),
        timezone="Europe/Warsaw",
        soc_start_pct=50.0,
        expected_total_cashflow_pln=0.0,
        optimizer="test",
        inputs_snapshot={},
        hours=[HourPlan(
            date="2026-06-10",
            hour=12,
            target_net_kwh=2.0,
            expected_cashflow_pln=0.0,
            soc_start_pct=50.0,
            soc_end_pct=55.0,
            battery_delta_kwh=0.0,
        )],
    )
    hin = [HourInputs(
        date="2026-06-10",
        hour=12,
        load_kwh=1.0,
        pv_kwh=3.0,
        import_pln_per_kwh=1.0,
        export_pln_per_kwh=0.4,
    )]
    save_policy_artifact(build_policy_artifact(plan, hin, valid_minutes=15))

    got = active_policy_row(now.date(), 12, now=now)
    assert got is not None
    row, art = got
    assert row.exec_mode == "export_pv_surplus"
    assert art.plan_id == "p1"


def test_active_policy_row_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import planner.policy_output as po_mod
    from planner.guardian_policy import active_policy_row

    out = tmp_path / "planner_output.json"
    monkeypatch.setattr(po_mod, "PLANNER_OUTPUT_PATH", out)

    old = datetime(2026, 6, 10, 10, 0, 0, tzinfo=UTC)
    plan = DailyPlan(
        plan_id="p-old",
        local_date="2026-06-10",
        generated_at=old.isoformat(),
        timezone="Europe/Warsaw",
        soc_start_pct=50.0,
        expected_total_cashflow_pln=0.0,
        optimizer="test",
        inputs_snapshot={},
        hours=[HourPlan(
            date="2026-06-10",
            hour=12,
            target_net_kwh=0.0,
            expected_cashflow_pln=0.0,
            soc_start_pct=50.0,
            soc_end_pct=50.0,
            battery_delta_kwh=0.0,
        )],
    )
    save_policy_artifact(
        build_policy_artifact(
            plan,
            [HourInputs(
                date="2026-06-10",
                hour=12,
                load_kwh=1.0,
                pv_kwh=1.0,
                import_pln_per_kwh=1.0,
                export_pln_per_kwh=0.4,
            )],
            valid_minutes=5,
        )
    )
    now = datetime(2026, 6, 10, 12, 10, 0)
    assert active_policy_row(now.date(), 12, now=now) is None
