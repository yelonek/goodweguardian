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

# CVaR: ``auto`` = kalibracja z telemetrii; liczba > 0 = stałe λ; 0 = wyłączone (p50).
_CVAR_LAMBDA_RAW = (os.environ.get("PLANNER_CVAR_LAMBDA") or "0").strip().lower()
PLANNER_CVAR_ALPHA = _float_env("PLANNER_CVAR_ALPHA", 0.90)
PLANNER_SCENARIO_WEIGHT_PESSIMISTIC = _float_env("PLANNER_SCENARIO_WEIGHT_PESSIMISTIC", 0.15)
PLANNER_SCENARIO_WEIGHT_BASE = _float_env("PLANNER_SCENARIO_WEIGHT_BASE", 0.70)
PLANNER_SCENARIO_WEIGHT_OPTIMISTIC = _float_env("PLANNER_SCENARIO_WEIGHT_OPTIMISTIC", 0.15)

PLANNER_CVAR_CALIBRATE_LOOKBACK_DAYS = _int_env("PLANNER_CVAR_CALIBRATE_LOOKBACK_DAYS", 28)
PLANNER_CVAR_CALIBRATE_CACHE_HOURS = _int_env("PLANNER_CVAR_CALIBRATE_CACHE_HOURS", 24)
PLANNER_CVAR_CALIBRATE_MIN_DAYS = _int_env("PLANNER_CVAR_CALIBRATE_MIN_DAYS", 5)
PLANNER_CVAR_CALIBRATE_MIN_DAY_HOURS = _int_env("PLANNER_CVAR_CALIBRATE_MIN_DAY_HOURS", 18)
PLANNER_CVAR_CALIBRATE_DEFAULT_LAMBDA = _float_env("PLANNER_CVAR_CALIBRATE_DEFAULT_LAMBDA", 1.0)


def _parse_cvar_grid() -> list[float]:
    raw = os.environ.get("PLANNER_CVAR_CALIBRATE_GRID", "0,0.25,0.5,1,1.5,2,3,5")
    out: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            continue
    return out or [0.0, 0.5, 1.0, 2.0]


PLANNER_CVAR_CALIBRATE_GRID = _parse_cvar_grid()


def cvar_lambda_mode() -> str:
    """``off`` | ``fixed`` | ``auto``."""
    if _CVAR_LAMBDA_RAW == "auto":
        return "auto"
    try:
        return "off" if float(_CVAR_LAMBDA_RAW) <= 0.0 else "fixed"
    except ValueError:
        return "off"


def fixed_cvar_lambda_value() -> float:
    try:
        return max(0.0, float(_CVAR_LAMBDA_RAW))
    except ValueError:
        return 0.0


def planner_risk_optimizer_enabled() -> bool:
    return cvar_lambda_mode() in ("fixed", "auto")

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
