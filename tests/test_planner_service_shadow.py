from __future__ import annotations

from datetime import datetime

import pytest

from planner.models import HourInputs


def test_shadow_mode_writes_candidate_but_only_primary_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import planner.config as cfg
    import planner.service as service

    now = datetime(2026, 9, 18, 15, 0, 0)
    slots = [("2026-09-18", 15), ("2026-09-18", 19)]
    inputs = [
        HourInputs(
            date=d,
            hour=h,
            load_kwh=0.0,
            load_kwh_p25=0.0,
            load_kwh_p75=0.0,
            pv_kwh=0.0,
            pv_kwh_p10=0.0,
            pv_kwh_p90=0.0,
            import_pln_per_kwh=1.10,
            export_pln_per_kwh=0.10 if h == 15 else 2.50,
        )
        for d, h in slots
    ]
    captured: dict = {}
    policy_ids: list[str] = []

    monkeypatch.setattr(cfg, "_SCENARIO_OPTIMIZER_RAW", "1")
    monkeypatch.setattr(cfg, "_OPTIMIZER_MODE_RAW", "shadow")
    monkeypatch.setattr(service, "priced_horizon_slots", lambda **_: slots)
    monkeypatch.setattr(
        service,
        "build_hour_inputs_for_slots",
        lambda *_args, **_kwargs: (inputs, {"source": "test"}),
    )
    monkeypatch.setattr(service, "night_grid_charge_carry_in", lambda *_: False)
    monkeypatch.setattr(service, "night_grid_stored_kwh", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(service, "save_plan", lambda plan: captured.setdefault("primary", plan))
    monkeypatch.setattr(
        service,
        "save_shadow_artifacts",
        lambda candidate, comparison: captured.update(
            candidate=candidate, comparison=comparison
        ),
    )
    monkeypatch.setattr(
        service, "save_policy_artifact", lambda artifact: policy_ids.append(artifact.plan_id)
    )
    monkeypatch.setattr(service, "append_audit", lambda *_: None)

    primary = service.build_rolling_plan(soc_start_pct=10.0, now=now)

    assert primary is not None
    assert primary.optimizer == "shared_battery_legacy_v1"
    assert captured["candidate"].optimizer == "stochastic_mpc_shadow_v1"
    assert captured["comparison"]["same_inputs"] is True
    assert len(primary.inputs_snapshot["hour_inputs"]) == 2
    assert policy_ids == [primary.plan_id]
