"""Konfiguracja planera (env + ścieżki audytu)."""

from __future__ import annotations

import os

from guardian_config import DATA_DIR, P_BATTERY_W, _float_env, _int_env

PLANNER_DIR = DATA_DIR / "planner"
PLANNER_AUDIT_DIR = PLANNER_DIR / "audit"
PLANNER_PLANS_DIR = PLANNER_DIR / "plans"
PLANNER_REVIEWS_DIR = PLANNER_DIR / "reviews"

# Pojemność magazynu [kWh] — do symulacji SOC w optymalizatorze
PLANNER_BATTERY_KWH = _float_env("PLANNER_BATTERY_KWH", 10.0)
PLANNER_BATTERY_ETA = _float_env("PLANNER_BATTERY_ETA", 0.92)
PLANNER_SOC_MIN_PCT = _float_env("PLANNER_SOC_MIN_PCT", 10.0)
PLANNER_SOC_MAX_PCT = _float_env("PLANNER_SOC_MAX_PCT", 100.0)
PLANNER_MAX_NET_KWH_PER_H = _float_env("PLANNER_MAX_NET_KWH_PER_H", 5.0)
PLANNER_NET_STEP_KWH = _float_env("PLANNER_NET_STEP_KWH", 0.25)
PLANNER_SOC_STEP_PCT = _int_env("PLANNER_SOC_STEP_PCT", 5)
PLANNER_HORIZON_HOURS = _int_env("PLANNER_HORIZON_HOURS", 24)
PLANNER_LOAD_LOOKBACK_DAYS = _int_env("PLANNER_LOAD_LOOKBACK_DAYS", 28)

# Maks. moc ładowania/rozładowania magazynu w godzinie [kWh]
def max_battery_kwh_per_hour() -> float:
    return max(0.1, float(P_BATTERY_W) / 1000.0)


def ensure_planner_dirs() -> None:
    for d in (PLANNER_AUDIT_DIR, PLANNER_PLANS_DIR, PLANNER_REVIEWS_DIR):
        d.mkdir(parents=True, exist_ok=True)
