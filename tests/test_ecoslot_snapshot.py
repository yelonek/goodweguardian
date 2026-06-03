"""Snapshot ecoslotów zapisywany przez runner."""

from datetime import datetime
from types import SimpleNamespace

import ecoslot_service as svc


def test_build_and_snapshot_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(svc, "ECOSLOTS_SNAPSHOT_PATH", tmp_path / "ecoslots_snapshot.json")
    monkeypatch.setattr(svc, "INVERTER_IP", "192.168.1.10")
    monkeypatch.setattr(svc, "ECO_SLOT_BALANCING", 4)

    now = datetime(2026, 6, 3, 14, 30)
    slots_raw = {
        "eco_mode_1": SimpleNamespace(
            start_h=8, start_m=0, end_h=12, end_m=0, power=-50,
            days="Mon-Fri", soc=100, on_off=-2,
        ),
        "eco_mode_2": None,
        "eco_mode_3": None,
        "eco_mode_4": SimpleNamespace(
            start_h=14, start_m=30, end_h=14, end_m=31, power=1,
            days="Mon-Sun", soc=100, on_off=-2,
        ),
    }
    supported = {"eco_mode_1", "eco_mode_4"}
    payload = svc.build_ecoslots_payload(
        slots_raw, now=now, source="runner", supported_ids=supported
    )
    svc.save_ecoslots_snapshot(payload)
    loaded = svc.load_ecoslots_payload_from_snapshot()
    assert loaded is not None
    assert loaded["source"] == "runner"
    assert loaded["slots"]["eco_mode_1"]["power_pct"] == -50
    assert loaded["slots"]["eco_mode_2"]["supported"] is False
