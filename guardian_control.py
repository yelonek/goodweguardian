"""Runtime włączenie/wyłączenie zapisu do inwertera: plik override lub domyślna wartość z env."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

import guardian_config as _gc

ControlSource = Literal["override", "env"]


def effective_control_enabled() -> tuple[bool, ControlSource]:
    """
    Jeśli istnieje poprawny plik override — użyj go.
    W przeciwnym razie GUARDIAN_CONTROL_ENABLED z env (ładowane przy starcie procesu).
    """
    path = _gc.GUARDIAN_CONTROL_OVERRIDE_PATH
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and "control_enabled" in data:
                return bool(data["control_enabled"]), "override"
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logging.getLogger("guardian").warning(
                "guardian_control_override read failed %s: %s", path, e
            )
    return _gc.GUARDIAN_CONTROL_ENABLED, "env"


def write_control_override(enabled: bool, path: Path | None = None) -> None:
    """Atomowy zapis {"control_enabled": bool}."""
    target = path or _gc.GUARDIAN_CONTROL_OVERRIDE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = json.dumps({"control_enabled": enabled}, indent=2) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target)
