"""Override pliku sterowania vs env."""

import json

import pytest

import guardian_config
import guardian_control


def test_effective_uses_env_when_no_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(guardian_config, "GUARDIAN_CONTROL_OVERRIDE_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(guardian_config, "GUARDIAN_CONTROL_ENABLED", True)
    en, src = guardian_control.effective_control_enabled()
    assert en is True
    assert src == "env"

    monkeypatch.setattr(guardian_config, "GUARDIAN_CONTROL_ENABLED", False)
    en, src = guardian_control.effective_control_enabled()
    assert en is False
    assert src == "env"


def test_effective_prefers_override_file(monkeypatch, tmp_path) -> None:
    path = tmp_path / "o.json"
    monkeypatch.setattr(guardian_config, "GUARDIAN_CONTROL_OVERRIDE_PATH", path)
    monkeypatch.setattr(guardian_config, "GUARDIAN_CONTROL_ENABLED", False)
    path.write_text(json.dumps({"control_enabled": True}), encoding="utf-8")
    en, src = guardian_control.effective_control_enabled()
    assert en is True
    assert src == "override"


def test_write_override_atomic(monkeypatch, tmp_path) -> None:
    path = tmp_path / "o.json"
    monkeypatch.setattr(guardian_config, "GUARDIAN_CONTROL_OVERRIDE_PATH", path)
    guardian_control.write_control_override(False)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["control_enabled"] is False
