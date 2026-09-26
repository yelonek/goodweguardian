"""Jeden przebieg JSONL: load, EV i cache po mtime."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from telemetry_scan import clear_telemetry_scan_cache, scan_telemetry_file


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_scan_load_pv_and_ev_from_one_pass(tmp_path: Path) -> None:
    day = date(2026, 6, 7)
    path = tmp_path / f"telemetry_{day.isoformat()}.jsonl"
    _write(
        path,
        [
            {
                "local_date": day.isoformat(),
                "local_hour": 10,
                "local_minute": 0,
                "consumption_w": 12000.0,
                "E_pv_kwh": 10.0,
                "E_twc_kwh": 100.0,
                "E_imp_kwh": 1.0,
                "E_exp_kwh": 2.0,
                "ts_utc": "2026-06-07T08:00:00Z",
            },
            {
                "local_date": day.isoformat(),
                "local_hour": 10,
                "local_minute": 30,
                "consumption_w": 8000.0,
                "E_pv_kwh": 10.4,
                "E_twc_kwh": 101.0,
                "ts_utc": "2026-06-07T08:30:00Z",
            },
            {
                "local_date": day.isoformat(),
                "local_hour": 11,
                "local_minute": 0,
                "consumption_w": 500.0,
                "E_pv_kwh": 11.5,
                "E_twc_kwh": 103.0,
                "ts_utc": "2026-06-07T09:00:00Z",
            },
        ],
    )
    scan = scan_telemetry_file(path)
    assert scan is not None
    assert scan.n_rows == 3
    assert scan.load_avg_kwh[10] == pytest.approx(10.0)
    assert scan.first_pv[10] == pytest.approx(10.0)
    assert scan.first_twc[10] == pytest.approx(100.0)
    assert scan.first_twc[11] == pytest.approx(103.0)
    edge = scan.first_counter[(day.isoformat(), 10)]
    assert edge[2] == pytest.approx(1.0)
    assert edge[3] == pytest.approx(2.0)


def test_scan_cache_skips_second_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "telemetry_2026-06-08.jsonl"
    _write(
        path,
        [{"local_hour": 1, "local_minute": 0, "consumption_w": 1000.0}],
    )
    clear_telemetry_scan_cache()
    first = scan_telemetry_file(path)
    assert first is not None

    def boom(*_a, **_k):
        raise AssertionError("drugi odczyt nie powinien otwierać pliku")

    monkeypatch.setattr(Path, "open", boom)
    second = scan_telemetry_file(path)
    assert second is first
