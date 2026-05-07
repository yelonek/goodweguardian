"""guardian_watchdog_override: merge pliku JSON z env guardian_config."""

from __future__ import annotations

import json

import pytest

import guardian_config as gc
from guardian_watchdog_override import (
    apply_watchdog_override_updates,
    clear_watchdog_override,
    effective_watchdog_soc,
    load_override_dict,
    watchdog_soc_api_payload,
)


@pytest.fixture
def isolated_override_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    p = tmp_path / "guardian_watchdog_override.json"
    monkeypatch.setattr(gc, "GUARDIAN_WATCHDOG_OVERRIDE_PATH", p)


def test_no_file_all_env(isolated_override_path) -> None:
    eff = effective_watchdog_soc()
    assert eff.sources["soc_night_reserve_pct"] == "env"
    assert eff.soc_night_reserve_pct == gc.SOC_NIGHT_RESERVE_PCT


def test_override_pct(isolated_override_path) -> None:
    path = gc.GUARDIAN_WATCHDOG_OVERRIDE_PATH
    path.write_text(json.dumps({"soc_night_reserve_pct": 33.0}), encoding="utf-8")
    eff = effective_watchdog_soc()
    assert eff.soc_night_reserve_pct == 33.0
    assert eff.sources["soc_night_reserve_pct"] == "override"


def test_apply_then_clear(isolated_override_path) -> None:
    apply_watchdog_override_updates({"soc_night_reserve_pct": 40.0})
    assert gc.GUARDIAN_WATCHDOG_OVERRIDE_PATH.exists()
    assert load_override_dict()["soc_night_reserve_pct"] == 40.0
    clear_watchdog_override()
    assert not gc.GUARDIAN_WATCHDOG_OVERRIDE_PATH.exists()


def test_null_removes_key(isolated_override_path) -> None:
    apply_watchdog_override_updates({"soc_night_reserve_pct": 41.0})
    apply_watchdog_override_updates({"soc_night_reserve_pct": None})
    assert not gc.GUARDIAN_WATCHDOG_OVERRIDE_PATH.exists()


def test_watchdog_soc_api_payload_shape(isolated_override_path) -> None:
    p = watchdog_soc_api_payload()
    assert set(p["effective"].keys()) == set(p["env_base"].keys())
    assert "sources" in p
    assert "override_path" in p
