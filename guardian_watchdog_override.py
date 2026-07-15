"""Runtime progi SOC watchdog — cienki widok nad ``guardian_settings``.

Historycznie osobny plik override; teraz te 6 pól to część jednolitego
``GuardianSettings`` (``state/settings.json``). Moduł zostaje jako warstwa
zgodności dla dashboardu (endpointy ``/api/guardian/watchdog-soc``) i testów.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import guardian_settings as _gs

Source = Literal["override", "default"]

# 6 pól SOC watchdog sterowanych z dedykowanego widoku dashboardu.
ALLOWED_KEYS = frozenset(
    {
        "soc_night_reserve_enabled",
        "soc_night_reserve_pct",
        "soc_night_reserve_charge_pct",
        "soc_night_reserve_hours",
        "soc_low_defense_threshold_pct",
        "soc_full_defense_threshold_pct",
    }
)


@dataclass(frozen=True)
class EffectiveWatchdogSoc:
    soc_night_reserve_enabled: bool
    soc_night_reserve_pct: float
    soc_night_reserve_charge_pct: int
    night_reserve_hours: frozenset[int]
    soc_low_defense_threshold_pct: float
    soc_full_defense_threshold_pct: float
    sources: dict[str, Source]


def default_watchdog_soc() -> EffectiveWatchdogSoc:
    """Wartości domyślne 6 pól watchdog SOC (schemat, bez override) — do payloadu ``defaults``."""
    d = _gs.GuardianSettings().model_dump()
    return EffectiveWatchdogSoc(
        soc_night_reserve_enabled=bool(d["soc_night_reserve_enabled"]),
        soc_night_reserve_pct=float(d["soc_night_reserve_pct"]),
        soc_night_reserve_charge_pct=int(d["soc_night_reserve_charge_pct"]),
        night_reserve_hours=frozenset(d["soc_night_reserve_hours"]),
        soc_low_defense_threshold_pct=float(d["soc_low_defense_threshold_pct"]),
        soc_full_defense_threshold_pct=float(d["soc_full_defense_threshold_pct"]),
        sources={k: "default" for k in ALLOWED_KEYS},
    )


def load_override_dict() -> dict[str, Any]:
    """Aktualne override tylko dla 6 pól watchdog SOC."""
    ov = _gs.current_overrides()
    return {k: v for k, v in ov.items() if k in ALLOWED_KEYS}


def effective_watchdog_soc() -> EffectiveWatchdogSoc:
    s = _gs.get_settings()
    src = _gs.sources()
    return EffectiveWatchdogSoc(
        soc_night_reserve_enabled=bool(s.soc_night_reserve_enabled),
        soc_night_reserve_pct=float(s.soc_night_reserve_pct),
        soc_night_reserve_charge_pct=int(s.soc_night_reserve_charge_pct),
        night_reserve_hours=frozenset(s.soc_night_reserve_hours),
        soc_low_defense_threshold_pct=float(s.soc_low_defense_threshold_pct),
        soc_full_defense_threshold_pct=float(s.soc_full_defense_threshold_pct),
        sources={k: src.get(k, "default") for k in ALLOWED_KEYS},  # type: ignore[misc]
    )


def watchdog_soc_api_payload() -> dict[str, Any]:
    """GET /api/guardian/watchdog-soc — jedna funkcja dla dashboardu i testów."""
    base = default_watchdog_soc()
    eff = effective_watchdog_soc()
    ov = load_override_dict()
    return {
        "override_path": str(_gs.settings_path()),
        "override_exists": bool(ov),
        "defaults": {
            "soc_night_reserve_enabled": base.soc_night_reserve_enabled,
            "soc_night_reserve_pct": base.soc_night_reserve_pct,
            "soc_night_reserve_charge_pct": base.soc_night_reserve_charge_pct,
            "soc_night_reserve_hours": sorted(base.night_reserve_hours),
            "soc_low_defense_threshold_pct": base.soc_low_defense_threshold_pct,
            "soc_full_defense_threshold_pct": base.soc_full_defense_threshold_pct,
        },
        "effective": {
            "soc_night_reserve_enabled": eff.soc_night_reserve_enabled,
            "soc_night_reserve_pct": eff.soc_night_reserve_pct,
            "soc_night_reserve_charge_pct": eff.soc_night_reserve_charge_pct,
            "soc_night_reserve_hours": sorted(eff.night_reserve_hours),
            "soc_low_defense_threshold_pct": eff.soc_low_defense_threshold_pct,
            "soc_full_defense_threshold_pct": eff.soc_full_defense_threshold_pct,
        },
        "sources": dict(eff.sources),
    }


def apply_watchdog_override_updates(updates: dict[str, Any]) -> None:
    """Merge 6 pól SOC do ``settings.json``. ``None`` usuwa klucz. Reszta ignorowana."""
    filtered = {k: v for k, v in updates.items() if k in ALLOWED_KEYS}
    if filtered:
        _gs.update_overrides(filtered)


def clear_watchdog_override() -> None:
    """Usuń override 6 pól watchdog SOC (powrót do env/default)."""
    _gs.reset_overrides(list(ALLOWED_KEYS))
