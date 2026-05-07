"""Runtime overrides dla progów SOC watchdog (plik JSON), bez restartu — jak guardian_control_override."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

import guardian_config as _gc

logger = logging.getLogger("guardian")

Source = Literal["override", "env"]

ALLOWED_KEYS = frozenset(
    {
        "soc_night_reserve_pct",
        "soc_night_reserve_charge_pct",
        "soc_night_reserve_hours",
        "soc_low_defense_threshold_pct",
        "soc_full_defense_threshold_pct",
    }
)


@dataclass(frozen=True)
class EffectiveWatchdogSoc:
    soc_night_reserve_pct: float
    soc_night_reserve_charge_pct: int
    night_reserve_hours: frozenset[int]
    soc_low_defense_threshold_pct: float
    soc_full_defense_threshold_pct: float
    sources: dict[str, Source]


def env_base_watchdog_soc() -> EffectiveWatchdogSoc:
    """Wartości z guardian_config (env przy imporcie), bez pliku override."""
    return EffectiveWatchdogSoc(
        soc_night_reserve_pct=float(_gc.SOC_NIGHT_RESERVE_PCT),
        soc_night_reserve_charge_pct=int(_gc.SOC_NIGHT_RESERVE_CHARGE_PCT),
        night_reserve_hours=frozenset(_gc.SOC_NIGHT_RESERVE_HOURS),
        soc_low_defense_threshold_pct=float(_gc.SOC_LOW_DEFENSE_THRESHOLD_PCT),
        soc_full_defense_threshold_pct=float(_gc.SOC_FULL_DEFENSE_THRESHOLD_PCT),
        sources={k: "env" for k in ALLOWED_KEYS},
    )


def _normalize_hours(val: Any) -> frozenset[int]:
    if isinstance(val, frozenset):
        seq = list(val)
    elif isinstance(val, (list, tuple, set)):
        seq = list(val)
    else:
        raise ValueError("soc_night_reserve_hours must be a list of hours")
    out: list[int] = []
    for x in seq:
        h = int(x)
        if not 0 <= h <= 23:
            raise ValueError(f"hour out of range 0..23: {h}")
        out.append(h)
    return frozenset(out)


def _coerce_from_file(key: str, val: Any) -> Any:
    if key == "soc_night_reserve_pct":
        x = float(val)
        if not 0.0 <= x <= 100.0:
            raise ValueError("soc_night_reserve_pct must be 0..100")
        return x
    if key == "soc_night_reserve_charge_pct":
        x = int(val)
        if not -1 <= x <= 100:
            raise ValueError("soc_night_reserve_charge_pct must be -1..100")
        return x
    if key == "soc_night_reserve_hours":
        return _normalize_hours(val)
    if key == "soc_low_defense_threshold_pct":
        x = float(val)
        if not 0.0 <= x <= 100.0:
            raise ValueError("soc_low_defense_threshold_pct must be 0..100")
        return x
    if key == "soc_full_defense_threshold_pct":
        x = float(val)
        if not 0.0 <= x <= 100.0:
            raise ValueError("soc_full_defense_threshold_pct must be 0..100")
        return x
    raise ValueError(f"unknown key {key}")


def load_override_dict() -> dict[str, Any]:
    path = _gc.GUARDIAN_WATCHDOG_OVERRIDE_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("guardian_watchdog_override read failed %s: %s", path, e)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in ALLOWED_KEYS:
            continue
        try:
            out[k] = _coerce_from_file(k, v)
        except (TypeError, ValueError) as e:
            logger.warning("guardian_watchdog_override skip key %s: %s", k, e)
    return out


def effective_watchdog_soc() -> EffectiveWatchdogSoc:
    base = env_base_watchdog_soc()
    ov = load_override_dict()
    sources: dict[str, Source] = {}

    def pick_float(name: str, base_v: float) -> float:
        if name in ov:
            sources[name] = "override"
            return float(ov[name])
        sources[name] = "env"
        return base_v

    def pick_int(name: str, base_v: int) -> int:
        if name in ov:
            sources[name] = "override"
            return int(ov[name])
        sources[name] = "env"
        return base_v

    if "soc_night_reserve_hours" in ov:
        nh: frozenset[int] = ov["soc_night_reserve_hours"]
        sources["soc_night_reserve_hours"] = "override"
    else:
        nh = base.night_reserve_hours
        sources["soc_night_reserve_hours"] = "env"

    return EffectiveWatchdogSoc(
        soc_night_reserve_pct=pick_float("soc_night_reserve_pct", base.soc_night_reserve_pct),
        soc_night_reserve_charge_pct=pick_int(
            "soc_night_reserve_charge_pct", base.soc_night_reserve_charge_pct
        ),
        night_reserve_hours=nh,
        soc_low_defense_threshold_pct=pick_float(
            "soc_low_defense_threshold_pct", base.soc_low_defense_threshold_pct
        ),
        soc_full_defense_threshold_pct=pick_float(
            "soc_full_defense_threshold_pct", base.soc_full_defense_threshold_pct
        ),
        sources=sources,
    )


def _serialize_for_json(hours: frozenset[int]) -> list[int]:
    return sorted(hours)


def watchdog_soc_api_payload() -> dict[str, Any]:
    """GET /api/guardian/watchdog-soc — jedna funkcja dla dashboardu i testów."""
    base = env_base_watchdog_soc()
    eff = effective_watchdog_soc()
    path = _gc.GUARDIAN_WATCHDOG_OVERRIDE_PATH
    return {
        "override_path": str(path),
        "override_exists": path.exists(),
        "env_base": {
            "soc_night_reserve_pct": base.soc_night_reserve_pct,
            "soc_night_reserve_charge_pct": base.soc_night_reserve_charge_pct,
            "soc_night_reserve_hours": _serialize_for_json(base.night_reserve_hours),
            "soc_low_defense_threshold_pct": base.soc_low_defense_threshold_pct,
            "soc_full_defense_threshold_pct": base.soc_full_defense_threshold_pct,
        },
        "effective": {
            "soc_night_reserve_pct": eff.soc_night_reserve_pct,
            "soc_night_reserve_charge_pct": eff.soc_night_reserve_charge_pct,
            "soc_night_reserve_hours": _serialize_for_json(eff.night_reserve_hours),
            "soc_low_defense_threshold_pct": eff.soc_low_defense_threshold_pct,
            "soc_full_defense_threshold_pct": eff.soc_full_defense_threshold_pct,
        },
        "sources": dict(eff.sources),
    }


def apply_watchdog_override_updates(updates: dict[str, Any]) -> None:
    """
    Merge into override file. Value None removes that key (fall back to env).
    Ignores keys outside ALLOWED_KEYS.
    """
    path = _gc.GUARDIAN_WATCHDOG_OVERRIDE_PATH
    current = load_override_dict()
    for k, v in updates.items():
        if k not in ALLOWED_KEYS:
            continue
        if v is None:
            current.pop(k, None)
            continue
        current[k] = _coerce_from_file(k, v)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not current:
        try:
            path.unlink()
        except OSError:
            pass
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(_dumpable(current), indent=2, sort_keys=True) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _dumpable(merged: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in sorted(merged.keys()):
        v = merged[k]
        if k == "soc_night_reserve_hours" and isinstance(v, frozenset):
            out[k] = sorted(v)
        else:
            out[k] = v
    return out


def clear_watchdog_override() -> None:
    path = _gc.GUARDIAN_WATCHDOG_OVERRIDE_PATH
    try:
        path.unlink()
    except OSError:
        pass
