"""Jednolity system ustawień: jedno źródło prawdy (settings.json), walidacja, hot-reload, API, migracja."""

from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

import guardian_settings as gs


@pytest.fixture
def tmp_settings(monkeypatch: pytest.MonkeyPatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("GUARDIAN_SETTINGS_PATH", str(path))
    gs.reset_cache()
    yield path
    gs.reset_cache()


def test_defaults_when_no_file(tmp_settings) -> None:
    eff = gs.get_settings()
    assert eff.model_dump() == gs.GuardianSettings().model_dump()
    assert all(v == "default" for v in gs.sources().values())
    assert gs.is_onboarding_completed() is False


def test_override_layer_precedence(tmp_settings) -> None:
    tmp_settings.write_text(json.dumps({"overrides": {"exec_steady_pct": 7}}), encoding="utf-8")
    gs.reset_cache()
    assert gs.get_settings().exec_steady_pct == 7
    assert gs.sources()["exec_steady_pct"] == "override"
    # inne pola nadal domyślne
    assert gs.sources()["battery_capacity_kwh"] == "default"


def test_unknown_key_ignored(tmp_settings) -> None:
    tmp_settings.write_text(
        json.dumps({"overrides": {"exec_steady_pct": 3, "nope": 1}}), encoding="utf-8"
    )
    gs.reset_cache()
    assert gs.get_settings().exec_steady_pct == 3
    assert "nope" not in gs.current_overrides()


def test_invalid_override_in_file_is_dropped(tmp_settings) -> None:
    tmp_settings.write_text(
        json.dumps({"overrides": {"soc_night_reserve_pct": 999, "exec_steady_pct": 4}}),
        encoding="utf-8",
    )
    gs.reset_cache()
    eff = gs.get_settings()
    # zły klucz pominięty, dobry zastosowany
    assert eff.exec_steady_pct == 4
    assert eff.soc_night_reserve_pct == gs.GuardianSettings().soc_night_reserve_pct


def test_update_validates_range(tmp_settings) -> None:
    with pytest.raises(ValidationError):
        gs.update_overrides({"soc_night_reserve_pct": 150})
    # nic się nie zapisało
    assert gs.current_overrides() == {}


def test_update_and_null_removes(tmp_settings) -> None:
    gs.update_overrides({"exec_steady_pct": 6})
    assert gs.get_settings().exec_steady_pct == 6
    gs.update_overrides({"exec_steady_pct": None})
    assert "exec_steady_pct" not in gs.current_overrides()
    assert gs.sources()["exec_steady_pct"] == "default"


def test_onboarding_marker(tmp_settings) -> None:
    assert gs.is_onboarding_completed() is False
    gs.update_overrides({}, complete_onboarding=True)
    assert gs.is_onboarding_completed() is True


def test_reset_all_overrides(tmp_settings) -> None:
    gs.update_overrides({"exec_steady_pct": 6, "battery_capacity_kwh": 12.0})
    assert gs.current_overrides()
    gs.reset_overrides()
    assert gs.current_overrides() == {}


def test_hot_reload_on_mtime(tmp_settings) -> None:
    base_val = gs.get_settings().exec_steady_pct
    t = 1_600_000_000

    tmp_settings.write_text(json.dumps({"overrides": {"exec_steady_pct": 7}}), encoding="utf-8")
    os.utime(tmp_settings, (t + 10, t + 10))
    assert gs.get_settings().exec_steady_pct == 7  # przeliczone po zmianie mtime

    tmp_settings.write_text(json.dumps({"overrides": {"exec_steady_pct": 9}}), encoding="utf-8")
    os.utime(tmp_settings, (t + 20, t + 20))
    assert gs.get_settings().exec_steady_pct == 9

    tmp_settings.unlink()
    assert gs.get_settings().exec_steady_pct == base_val  # powrót do domyślnej po usunięciu pliku


def test_schema_has_group_and_description(tmp_settings) -> None:
    schema = gs.settings_schema()
    props = schema["properties"]
    assert len(props) == len(gs.GuardianSettings.model_fields)
    sample = props["exec_steady_pct"]
    assert sample.get("group") == "execution"
    assert sample.get("description")


def test_api_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_settings) -> None:
    from fastapi.testclient import TestClient

    import guardian_config as gc
    import guardian_dashboard as d

    monkeypatch.setattr(gc, "GUARDIAN_API_KEY", "k")
    client = TestClient(d.app)

    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["onboarding_completed"] is False

    # brak klucza -> 401
    assert client.put("/api/settings", json={"overrides": {"exec_steady_pct": 5}}).status_code == 401

    r = client.put(
        "/api/settings",
        headers={"X-Guardian-Api-Key": "k"},
        json={"overrides": {"exec_steady_pct": 5}, "complete_onboarding": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["effective"]["exec_steady_pct"] == 5
    assert body["sources"]["exec_steady_pct"] == "override"
    assert body["onboarding_completed"] is True

    # walidacja zakresu -> 422
    assert client.put(
        "/api/settings",
        headers={"X-Guardian-Api-Key": "k"},
        json={"overrides": {"soc_night_reserve_pct": 999}},
    ).status_code == 422

    # reset jednego klucza
    r = client.delete("/api/settings?keys=exec_steady_pct", headers={"X-Guardian-Api-Key": "k"})
    assert r.json()["sources"]["exec_steady_pct"] == "default"
