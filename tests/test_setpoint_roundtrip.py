"""Nastawa w watach wraca na ten sam procent slotu GoodWe."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from decision.logic import WatchdogDecision, setpoint_from_pct
from decision.setpoint import slot_pct_from_watts
from goodwe_adapter import eco_slot_command

ROOT = Path(__file__).resolve().parents[1]


def test_integer_percent_round_trip() -> None:
    watts_per_percent = 70.0
    for pct in range(-100, 101):
        decision = WatchdogDecision(
            write_slot=pct != 0,
            enabled=pct != 0,
            power_pct=pct,
            duration_s=60.0,
            mode="discharge" if pct > 0 else "charge",
            reason="round_trip",
            slot_soc_pct=80,
        )
        setpoint = setpoint_from_pct(decision, watts_per_percent)
        assert setpoint.power_w == pct * watts_per_percent
        command = eco_slot_command(setpoint, watts_per_percent)
        assert command.power_pct == pct
        assert command.slot_soc_pct == 80
        assert command.write_slot is (pct != 0)
        assert slot_pct_from_watts(setpoint.power_w, watts_per_percent) == pct


def test_release_is_zero_percent_slot() -> None:
    decision = WatchdogDecision(
        write_slot=False,
        enabled=False,
        power_pct=0,
        duration_s=0.0,
        mode="neutral",
        reason="neutral_hold",
    )
    setpoint = setpoint_from_pct(decision, 70.0)
    assert setpoint.release is True
    command = eco_slot_command(setpoint, 70.0)
    assert command.write_slot is False
    assert command.power_pct == 0


def test_decision_package_does_not_import_goodwe_or_ecoslot() -> None:
    decision_dir = ROOT / "decision"
    for path in decision_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = ""
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [alias.name for alias in node.names]
            else:
                continue
            blob = " ".join([module, *names])
            assert "goodwe" not in blob
            assert "ecoslot" not in blob


def test_slot_pct_rejects_non_positive_scale() -> None:
    with pytest.raises(ValueError):
        slot_pct_from_watts(70.0, 0.0)
