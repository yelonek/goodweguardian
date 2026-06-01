"""History: bilans końcowy poprzedniej godziny (HH:00) liczony z liczników telemetrii."""

import json
from datetime import datetime

import pytest

import guardian_dashboard
from guardian_dashboard import DashboardRow, annotate_history_closing_balance


def _row(ts: str) -> DashboardRow:
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    # remaining_kwh=0.0 jak realny live na pełnej godzinie (po resecie runnera)
    return DashboardRow(ts=dt, raw=ts, fields={"ts": ts, "remaining_kwh": 0.0})


@pytest.fixture
def telemetry_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(guardian_dashboard, "TELEMETRY_DIR", tmp_path)
    return tmp_path


def _write_telemetry(dir_path, local_date: str, records: list[dict]) -> None:
    path = dir_path / f"telemetry_{local_date}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _rec(ts_utc: str, local_date: str, hour: int, imp: float, exp: float) -> dict:
    return {
        "ts_utc": ts_utc,
        "local_date": local_date,
        "local_hour": hour,
        "E_imp_kwh": imp,
        "E_exp_kwh": exp,
    }


def test_full_hour_row_shows_counter_based_closing(telemetry_dir) -> None:
    # Godzina 7: Δexp=0.9, Δimp=0.5 → net = +0.4
    _write_telemetry(
        telemetry_dir,
        "2026-05-31",
        [
            _rec("2026-05-31T05:00:00Z", "2026-05-31", 7, 100.0, 200.0),
            _rec("2026-05-31T06:00:00Z", "2026-05-31", 8, 100.5, 200.9),
        ],
    )
    rows = [
        _row("2026-05-31 08:00:00"),
        _row("2026-05-31 07:59:00"),
    ]
    annotate_history_closing_balance(rows)
    assert rows[0].fields["closing_prev_hour_kwh"] == pytest.approx(0.4)
    # remaining_kwh nietknięty (Current state ma pokazywać live)
    assert rows[0].fields["remaining_kwh"] == 0.0
    # wiersz nie-pełnej godziny bez bilansu końcowego
    assert rows[1].fields["closing_prev_hour_kwh"] is None


def test_missing_telemetry_yields_none(telemetry_dir) -> None:
    rows = [_row("2026-05-31 08:00:00")]
    annotate_history_closing_balance(rows)
    assert rows[0].fields["closing_prev_hour_kwh"] is None


def test_incomplete_interval_yields_none(telemetry_dir) -> None:
    # Jest start godziny 7, brak pierwszego odczytu godziny 8 → interwał niekompletny.
    _write_telemetry(
        telemetry_dir,
        "2026-05-31",
        [_rec("2026-05-31T05:00:00Z", "2026-05-31", 7, 100.0, 200.0)],
    )
    rows = [_row("2026-05-31 08:00:00")]
    annotate_history_closing_balance(rows)
    assert rows[0].fields["closing_prev_hour_kwh"] is None


def test_midnight_crosses_day_boundary(telemetry_dir) -> None:
    # :00 o północy → bilans godziny 23 dnia poprzedniego (start 23, koniec = 00 dnia +1)
    _write_telemetry(
        telemetry_dir,
        "2026-05-30",
        [_rec("2026-05-30T21:00:00Z", "2026-05-30", 23, 50.0, 70.0)],
    )
    _write_telemetry(
        telemetry_dir,
        "2026-05-31",
        [_rec("2026-05-30T22:00:00Z", "2026-05-31", 0, 50.3, 70.0)],
    )
    rows = [_row("2026-05-31 00:00:00")]
    annotate_history_closing_balance(rows)
    # Δexp=0.0, Δimp=0.3 → net = -0.3
    assert rows[0].fields["closing_prev_hour_kwh"] == pytest.approx(-0.3)
