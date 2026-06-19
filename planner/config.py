"""Konfiguracja planera (env + ścieżki audytu)."""

from __future__ import annotations

import os

from guardian_config import DATA_DIR, P_BATTERY_W, STATE_DIR, _float_env, _int_env

PLANNER_DIR = DATA_DIR / "planner"
PLANNER_AUDIT_DIR = PLANNER_DIR / "audit"
PLANNER_PLANS_DIR = PLANNER_DIR / "plans"
PLANNER_PLANS_HISTORY_DIR = PLANNER_PLANS_DIR / "history"
PLANNER_REVIEWS_DIR = PLANNER_DIR / "reviews"
PLANNER_AUDITS_DIR = PLANNER_DIR / "audits"
PLANNER_LATEST_PLAN_PATH = PLANNER_PLANS_DIR / "plan_latest.json"
PLANNER_OUTPUT_PATH = STATE_DIR / "planner_output.json"
PLANNER_POLICY_VALID_MINUTES = _int_env("PLANNER_POLICY_VALID_MINUTES", 10)
# Eksport zarobkowy: minimalna cena RCE [PLN/kWh] do trybu ``export_profit``.
PLANNER_EXPORT_PROFIT_MIN_PLN = _float_env("PLANNER_EXPORT_PROFIT_MIN_PLN", 0.35)

# Pojemność magazynu [kWh] — do symulacji SOC w optymalizatorze
PLANNER_BATTERY_KWH = _float_env("PLANNER_BATTERY_KWH", 10.0)
PLANNER_BATTERY_ETA = _float_env("PLANNER_BATTERY_ETA", 0.92)
# Amortyzacja: PLN za każdy kWh **rozładowania** magazynu (ład bez kary wear).
PLANNER_BATTERY_CYCLE_COST_PLN = _float_env("PLANNER_BATTERY_CYCLE_COST_PLN", 0.10)
PLANNER_SOC_MIN_PCT = _float_env("PLANNER_SOC_MIN_PCT", 10.0)
PLANNER_SOC_MAX_PCT = _float_env("PLANNER_SOC_MAX_PCT", 100.0)
PLANNER_HORIZON_HOURS = _int_env("PLANNER_HORIZON_HOURS", 24)
PLANNER_LOAD_LOOKBACK_DAYS = _int_env("PLANNER_LOAD_LOOKBACK_DAYS", 28)

# Wieloscenariuszowy MILP (p10/p50/p90); ``off`` = deterministyczny p50.
_SCENARIO_OPTIMIZER_RAW = (os.environ.get("PLANNER_SCENARIO_OPTIMIZER") or "1").strip().lower()
PLANNER_SCENARIO_WEIGHT_PESSIMISTIC = _float_env("PLANNER_SCENARIO_WEIGHT_PESSIMISTIC", 0.15)
PLANNER_SCENARIO_WEIGHT_BASE = _float_env("PLANNER_SCENARIO_WEIGHT_BASE", 0.70)
PLANNER_SCENARIO_WEIGHT_OPTIMISTIC = _float_env("PLANNER_SCENARIO_WEIGHT_OPTIMISTIC", 0.15)


def planner_scenario_optimizer_enabled() -> bool:
    return _SCENARIO_OPTIMIZER_RAW not in ("0", "off", "false", "no", "deterministic")


# Maks. moc ładowania/rozładowania magazynu w godzinie [kWh]
def max_battery_kwh_per_hour() -> float:
    return max(0.1, float(P_BATTERY_W) / 1000.0)


def ensure_planner_dirs() -> None:
    for d in (
        PLANNER_AUDIT_DIR,
        PLANNER_PLANS_DIR,
        PLANNER_PLANS_HISTORY_DIR,
        PLANNER_REVIEWS_DIR,
        PLANNER_AUDITS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
