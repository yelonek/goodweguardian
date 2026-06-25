"""Testy odczytu Tesla Wall Connector Gen 3."""

from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest

import guardian_state as gs
import tesla_wall_charger as twc


def test_twc_disabled_without_host(monkeypatch) -> None:
    monkeypatch.setattr(twc, "TESLA_WC_HOST", "")
    assert twc.twc_enabled() is False
    assert twc.fetch_lifetime_energy_kwh() is None


def test_fetch_lifetime_energy_kwh(monkeypatch) -> None:
    monkeypatch.setattr(twc, "TESLA_WC_HOST", "192.168.1.50")

    payload = {
        "energy_wh": 12125146,
        "charge_starts": 3044,
        "uptime_s": 84847077,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/1/lifetime"
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        kwh = twc.fetch_lifetime_energy_kwh(client=client)
    assert kwh == pytest.approx(12125.146)


def test_fetch_lifetime_https_host(monkeypatch) -> None:
    monkeypatch.setattr(twc, "TESLA_WC_HOST", "http://twc.local")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("http://twc.local/api/1/lifetime")
        return httpx.Response(200, json={"energy_wh": 1000})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        assert twc.fetch_lifetime_energy_kwh(client=client) == pytest.approx(1.0)


def test_compute_delta_twc_kwh_non_negative() -> None:
    assert twc.compute_delta_twc_kwh(10.5, hour_start_kwh=10.0) == pytest.approx(0.5)
    assert twc.compute_delta_twc_kwh(9.9, hour_start_kwh=10.0) == 0.0


def test_hour_start_twc_kwh_from_telemetry(monkeypatch, tmp_path) -> None:
    tel = tmp_path / "telemetry"
    tel.mkdir()
    monkeypatch.setattr(twc, "TELEMETRY_DIR", tel)
    day = "2026-06-19"
    path = tel / f"telemetry_{day}.jsonl"
    rows = [
        {
            "local_hour": 14,
            "local_minute": 5,
            "E_twc_kwh": 100.0,
        },
        {
            "local_hour": 14,
            "local_minute": 2,
            "E_twc_kwh": 99.5,
        },
        {
            "local_hour": 15,
            "local_minute": 0,
            "E_twc_kwh": 101.0,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    now = datetime(2026, 6, 19, 14, 30)
    assert twc.hour_start_twc_kwh_from_telemetry(now) == pytest.approx(99.5)


def test_load_and_save_twc_start(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setattr(gs, "STATE_DIR", state_dir)
    now = datetime(2026, 6, 19, 14, 15)
    gs.save_state(now, 1.0, 2.0, E_twc_start=12125.0)
    assert gs.load_twc_start(now) == pytest.approx(12125.0)
    assert gs.load_state(now) == (1.0, 2.0)


def test_hourly_ev_kwh_from_telemetry_boundary(monkeypatch, tmp_path) -> None:
    tel = tmp_path / "telemetry"
    tel.mkdir()
    monkeypatch.setattr(twc, "TELEMETRY_DIR", tel)
    day = date(2026, 6, 20)
    path = tel / f"telemetry_{day.isoformat()}.jsonl"
    rows = [
        {"local_hour": 10, "local_minute": 2, "E_twc_kwh": 100.0},
        {"local_hour": 10, "local_minute": 40, "E_twc_kwh": 101.0},
        {"local_hour": 11, "local_minute": 1, "E_twc_kwh": 102.5},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    ev = twc.hourly_ev_kwh_from_telemetry(day)
    assert ev[10] == pytest.approx(2.5)
    assert ev[11] == pytest.approx(0.0)


def test_hourly_ev_kwh_from_telemetry_max_delta_fallback(monkeypatch, tmp_path) -> None:
    tel = tmp_path / "telemetry"
    tel.mkdir()
    monkeypatch.setattr(twc, "TELEMETRY_DIR", tel)
    day = date(2026, 6, 20)
    path = tel / f"telemetry_{day.isoformat()}.jsonl"
    rows = [
        {"local_hour": 8, "local_minute": 10, "E_twc_kwh": 50.0, "delta_twc_kwh": 0.3},
        {"local_hour": 8, "local_minute": 45, "E_twc_kwh": 50.0, "delta_twc_kwh": 0.9},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    ev = twc.hourly_ev_kwh_from_telemetry(day)
    assert ev[8] == pytest.approx(0.9)


def test_hourly_ev_kwh_from_telemetry_empty_without_samples(monkeypatch, tmp_path) -> None:
    tel = tmp_path / "telemetry"
    tel.mkdir()
    monkeypatch.setattr(twc, "TELEMETRY_DIR", tel)
    day = date(2026, 6, 20)
    path = tel / f"telemetry_{day.isoformat()}.jsonl"
    path.write_text(
        json.dumps({"local_hour": 9, "local_minute": 0, "consumption_w": 500.0}) + "\n",
        encoding="utf-8",
    )
    assert twc.hourly_ev_kwh_from_telemetry(day) == {}
