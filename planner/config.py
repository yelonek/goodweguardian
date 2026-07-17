"""Konfiguracja planera (env + ścieżki audytu)."""

from __future__ import annotations

from guardian_config import DATA_DIR, P_BATTERY_W, STATE_DIR
from guardian_settings import get_settings

PLANNER_DIR = DATA_DIR / "planner"
PLANNER_AUDIT_DIR = PLANNER_DIR / "audit"
PLANNER_PLANS_DIR = PLANNER_DIR / "plans"
PLANNER_PLANS_HISTORY_DIR = PLANNER_PLANS_DIR / "history"
PLANNER_REVIEWS_DIR = PLANNER_DIR / "reviews"
PLANNER_AUDITS_DIR = PLANNER_DIR / "audits"
PLANNER_LATEST_PLAN_PATH = PLANNER_PLANS_DIR / "plan_latest.json"
PLANNER_OUTPUT_PATH = STATE_DIR / "planner_output.json"

# Wartości strojenia z jednolitego systemu ustawień (settings.json via get_settings()).
# Pozostają stałymi modułu (spójność z konsumentami/testami); świeży proces CLI
# planera czyta bieżące settings.json przy imporcie.
_s = get_settings()
PLANNER_POLICY_VALID_MINUTES = _s.planner_policy_valid_minutes

# Pojemność magazynu [kWh] — do symulacji SOC w optymalizatorze
PLANNER_BATTERY_KWH = _s.battery_capacity_kwh
PLANNER_BATTERY_ETA = _s.planner_battery_eta
# Amortyzacja: PLN za każdy kWh **rozładowania** magazynu (ład bez kary wear).
PLANNER_BATTERY_CYCLE_COST_PLN = _s.planner_battery_cycle_cost_pln
PLANNER_SOC_MIN_PCT = _s.planner_soc_min_pct
PLANNER_SOC_MAX_PCT = _s.planner_soc_max_pct
PLANNER_LOAD_LOOKBACK_DAYS = _s.planner_load_lookback_days

# Wieloscenariuszowy MILP (p10/p50/p90); ``off`` = deterministyczny p50.
_SCENARIO_OPTIMIZER_RAW = "1" if _s.planner_scenario_optimizer else "off"
PLANNER_SCENARIO_WEIGHT_PESSIMISTIC = _s.planner_scenario_weight_pessimistic
PLANNER_SCENARIO_WEIGHT_BASE = _s.planner_scenario_weight_base
PLANNER_SCENARIO_WEIGHT_OPTIMISTIC = _s.planner_scenario_weight_optimistic
_SOC_TRACKING_RAW = "1" if _s.planner_soc_tracking else "off"
PLANNER_SOC_TRACKING_LAMBDA = float(_s.planner_soc_tracking_lambda)


def planner_scenario_optimizer_enabled() -> bool:
    return _SCENARIO_OPTIMIZER_RAW not in ("0", "off", "false", "no", "deterministic")


def planner_soc_tracking_enabled() -> bool:
    """Tracking-SP (SOC* first-stage); wyłączenie = legacy shared ch/dis."""
    return _SOC_TRACKING_RAW not in ("0", "off", "false", "no")

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
