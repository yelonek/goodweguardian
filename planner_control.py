"""Czy Guardian egzekwuje rolling plan (override lub env). Plan jest liczony zawsze."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

import guardian_config as _gc

PlannerExecutionSource = Literal["override", "env"]


def _read_override_flag(data: dict) -> bool | None:
    if "planner_execution_enabled" in data:
        return bool(data["planner_execution_enabled"])
    if "planner_enabled" in data:
        return bool(data["planner_enabled"])
    return None


def effective_planner_execution_enabled() -> tuple[bool, PlannerExecutionSource]:
    """
    Guardian używa ``target_net_kwh`` z plan_latest gdy True.
    Sam planer (``planner plan``) działa niezależnie od tego przełącznika.
    """
    path = _gc.PLANNER_OVERRIDE_PATH
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                flag = _read_override_flag(data)
                if flag is not None:
                    return flag, "override"
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logging.getLogger("planner").warning(
                "planner_control_override read failed %s: %s", path, e
            )
    return _gc.PLANNER_EXECUTION_ENABLED, "env"


def write_planner_execution_override(enabled: bool, path: Path | None = None) -> None:
    """Atomowy zapis ``{"planner_execution_enabled": bool}``."""
    target = path or _gc.PLANNER_OVERRIDE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = json.dumps({"planner_execution_enabled": enabled}, indent=2) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target)


# Alias dla dashboardu / starszych importów
def effective_planner_enabled() -> tuple[bool, PlannerExecutionSource]:
    return effective_planner_execution_enabled()


def write_planner_override(enabled: bool, path: Path | None = None) -> None:
    write_planner_execution_override(enabled, path=path)
