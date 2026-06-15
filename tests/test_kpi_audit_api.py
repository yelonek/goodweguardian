"""Testy KPI + audyt: load/get_day_audit, merge, API."""

from __future__ import annotations

from datetime import date

import pytest

import guardian_dashboard as gd
import planner.day_audit as day_audit_mod
from planner.day_audit import get_day_audit, load_day_audit, save_day_audit
from planner.models import DayAudit, HourAuditRow


def _sample_audit(local_date: str = "2026-06-01") -> DayAudit:
    return DayAudit(
        local_date=local_date,
        audited_at="2026-06-02T00:30:00+00:00",
        actual_total_cashflow_pln=12.5,
        perfect_foresight_cashflow_pln=15.0,
        uplift_vs_actual_pln=2.5,
        hours=[
            HourAuditRow(
                hour=10,
                actual_net_kwh=0.4,
                actual_cashflow_pln=0.8,
                actual_load_kwh=1.0,
                actual_pv_kwh=1.5,
                optimal_net_kwh=0.6,
                optimal_cashflow_pln=1.1,
                gap_vs_optimal_pln=0.3,
            )
        ],
        summary_pl="test summary",
    )


def test_load_day_audit_roundtrip(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(day_audit_mod, "PLANNER_AUDITS_DIR", tmp_path)
    audit = _sample_audit()
    save_day_audit(audit)
    loaded = load_day_audit(date(2026, 6, 1))
    assert loaded is not None
    assert loaded.local_date == "2026-06-01"
    assert loaded.actual_total_cashflow_pln == pytest.approx(12.5)
    assert loaded.hours[0].hour == 10


def test_get_day_audit_saved_first(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(day_audit_mod, "PLANNER_AUDITS_DIR", tmp_path)
    save_day_audit(_sample_audit())
    audit, source = get_day_audit(date(2026, 6, 1))
    assert source == "saved"
    assert audit is not None
    assert audit.uplift_vs_actual_pln == pytest.approx(2.5)


def test_get_day_audit_missing_without_recompute(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(day_audit_mod, "PLANNER_AUDITS_DIR", tmp_path)
    audit, source = get_day_audit(date(2026, 6, 1), recompute_if_missing=False)
    assert audit is None
    assert source == "missing"


def test_get_day_audit_recomputes_when_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(day_audit_mod, "PLANNER_AUDITS_DIR", tmp_path)
    fake = _sample_audit("2026-06-02")
    monkeypatch.setattr(day_audit_mod, "hourly_actuals", lambda _d: {10: {"net_kwh": 0.4}})
    monkeypatch.setattr(day_audit_mod, "build_day_audit", lambda _d: fake)
    audit, source = get_day_audit(date(2026, 6, 2))
    assert source == "recomputed"
    assert audit is fake


def test_merge_kpi_audit_hours() -> None:
    kpi = {
        "hours": [
            {
                "hour": 10,
                "net_kwh": 0.35,
                "deposit_add_pln": 0.7,
                "electricity_bill_add_pln": None,
                "interval_complete": True,
            }
        ]
    }
    audit = _sample_audit()
    merged = gd._merge_kpi_audit_hours(kpi, audit)
    assert len(merged) == 24
    row10 = merged[10]
    assert row10["kpi_net_kwh"] == pytest.approx(0.35)
    assert row10["audit_net_kwh"] == pytest.approx(0.4)
    assert row10["gap_vs_optimal_pln"] == pytest.approx(0.3)
    row0 = merged[0]
    assert row0["kpi_net_kwh"] is None
    assert row0["audit_net_kwh"] is None


def test_api_kpi_day_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    d = date(2026, 6, 1)
    payload = {
        "date": d.isoformat(),
        "kpi": {
            "totals": {"net_cashflow_pln_day": 1.0},
            "hours": [],
            "warnings": [],
            "telemetry_rows": 0,
            "pricing_source": "test",
        },
        "audit": _sample_audit().model_dump(),
        "audit_source": "saved",
        "merged_hours": gd._merge_kpi_audit_hours(
            {"hours": []}, _sample_audit()
        ),
    }
    monkeypatch.setattr(gd, "_kpi_day_cache", {})
    monkeypatch.setattr(gd, "_kpi_day_payload", lambda local_date: payload)

    client = TestClient(gd.app)
    resp = client.get(f"/api/kpi/day?day={d.isoformat()}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == d.isoformat()
    assert body["audit_source"] == "saved"
    assert len(body["merged_hours"]) == 24
    assert body["kpi"]["totals"]["net_cashflow_pln_day"] == pytest.approx(1.0)
