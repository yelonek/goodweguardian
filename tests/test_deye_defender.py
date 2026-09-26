"""DeyeDefender nie startuje pętli i nie zapisuje, dopóki nie ma mapy rejestrów."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from decision.setpoint import Setpoint
from deye_defender.adapter import MAP_MISSING, DeyeDefender, DeyeMapMissing, main

ROOT = Path(__file__).resolve().parents[1]


def test_main_exits_before_any_loop() -> None:
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1


def test_port_refuses_read_and_write() -> None:
    defender = DeyeDefender()
    with pytest.raises(DeyeMapMissing, match=MAP_MISSING):
        asyncio.run(defender.read_balance_inputs())
    setpoint = Setpoint(
        power_w=70.0,
        soc_limit_pct=50,
        release=False,
        reason="charge_grid",
        mode="charge",
        duration_s=60.0,
    )
    with pytest.raises(DeyeMapMissing, match=MAP_MISSING):
        asyncio.run(defender.apply(setpoint))


def test_deye_package_does_not_import_goodwe() -> None:
    package = ROOT / "deye_defender"
    for path in package.glob("*.py"):
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
