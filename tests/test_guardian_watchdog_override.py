"""guardian_watchdog_override: widok pól SOC panelu nad guardian_settings (settings.json)."""

from __future__ import annotations

import json

import pytest

import guardian_settings as gs
from guardian_watchdog_override import (
    apply_watchdog_override_updates,
    clear_watchdog_override,
    effective_watchdog_soc,
    load_override_dict,
    watchdog_soc_api_payload,
)


@pytest.fixture
def isolated_override_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    p = tmp_path / "settings.json"
    monkeypatch.setenv("GUARDIAN_SETTINGS_PATH", str(p))
    gs.reset_cache()
    yield
    gs.reset_cache()


def _write_overrides(overrides: dict) -> None:
    path = gs.settings_path()
    path.write_text(json.dumps({"overrides": overrides}), encoding="utf-8")
    gs.reset_cache()


def test_no_file_all_default(isolated_override_path) -> None:
    eff = effective_watchdog_soc()
    assert eff.sources["soc_night_reserve_pct"] == "default"
    assert eff.soc_night_reserve_pct == gs.GuardianSettings().soc_night_reserve_pct


def test_override_pct(isolated_override_path) -> None:
    _write_overrides({"soc_night_reserve_pct": 33.0})
    eff = effective_watchdog_soc()
    assert eff.soc_night_reserve_pct == 33.0
    assert eff.sources["soc_night_reserve_pct"] == "override"


def test_apply_then_clear(isolated_override_path) -> None:
    apply_watchdog_override_updates({"soc_night_reserve_pct": 40.0})
    assert load_override_dict()["soc_night_reserve_pct"] == 40.0
    clear_watchdog_override()
    assert load_override_dict() == {}


def test_null_removes_key(isolated_override_path) -> None:
    apply_watchdog_override_updates({"soc_night_reserve_pct": 41.0})
    apply_watchdog_override_updates({"soc_night_reserve_pct": None})
    assert "soc_night_reserve_pct" not in load_override_dict()


def test_override_enabled_bool(isolated_override_path) -> None:
    apply_watchdog_override_updates({"soc_night_reserve_enabled": False})
    eff = effective_watchdog_soc()
    assert eff.soc_night_reserve_enabled is False
    assert eff.sources["soc_night_reserve_enabled"] == "override"


def test_watchdog_soc_api_payload_shape(isolated_override_path) -> None:
    p = watchdog_soc_api_payload()
    assert set(p["effective"].keys()) == set(p["defaults"].keys())
    assert "sources" in p
    assert "override_path" in p
