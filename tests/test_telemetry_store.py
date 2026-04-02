"""Zapis JSONL telemetrii."""

import json

from telemetry_store import CycleTelemetryRecord, append_cycle_record


def test_append_cycle_record_writes_jsonl(monkeypatch, tmp_path) -> None:
    tel = tmp_path / "tel"
    monkeypatch.setattr("telemetry_store.TELEMETRY_DIR", tel)
    rec = CycleTelemetryRecord(
        ts_utc="2026-01-15T12:00:00+00:00",
        local_date="2026-01-15",
        local_hour=13,
        local_minute=0,
        weekday=3,
        is_weekend=False,
        grid_w=100.0,
        pv_w=2000.0,
        battery_w=0.0,
        consumption_w=500.0,
        soc_pct=80.0,
        E_imp_kwh=1.0,
        E_exp_kwh=2.0,
        remaining_kwh=0.1,
        time_to_end_s=1800.0,
        delta_imp_kwh=0.05,
        delta_exp_kwh=0.06,
        slot_balancing_active=False,
        other_eco_active=False,
        ecoslot_pct=None,
        watchdog_write_slot=False,
        watchdog_reason="early_window_no_intervention",
        guardian_control_enabled=True,
        control_source="env",
        cmd_enabled=False,
        cmd_pct=0,
        cmd_duration_s=0.0,
    )
    append_cycle_record(rec)
    path = tel / "telemetry_2026-01-15.jsonl"
    assert path.exists()
    line = path.read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert row["schema_version"] == 1
    assert row["watchdog_reason"] == "early_window_no_intervention"
    assert row["guardian_control_enabled"] is True
