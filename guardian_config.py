"""Konfiguracja infrastruktury / bootstrap Guardiana z .env.

Tu trafiają WYŁĄCZNIE zmienne infrastruktury/sekretów (IP, klucze, proxy, ścieżki,
przełączniki operacyjne). Pokrętła strojenia są w schemacie ``guardian_settings`` i
zapisywane w ``state/settings.json`` — nie ma ich tutaj ani w ``.env``.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# Moc [W] – w .env w watach (int lub float z kropką)
def _float_env(name: str, default: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        if default is not None:
            return default
        raise ValueError(f"Brak wymaganej zmiennej środowiskowej: {name}")
    return float(raw.replace(",", "."))


def _int_env(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        if default is not None:
            return default
        raise ValueError(f"Brak wymaganej zmiennej środowiskowej: {name}")
    return int(float(raw.replace(",", ".")))


def _bool_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


# Wymagane
INVERTER_IP = os.environ.get("INVERTER_IP") or ""
ECO_SLOT_BALANCING = _int_env("ECO_SLOT_BALANCING", 4)
P_INVERTER_W = _float_env("P_INVERTER", 8200.0)
P_BATTERY_W = _float_env("P_BATTERY", 5000.0)

# Próg mocy bilansowania [kW] — wyświetlanie w logu dashboardu
BALANCE_POWER_THRESHOLD_KW = _float_env("BALANCE_POWER_THRESHOLD_KW", 0.3)

# ~70 W na 1% (plan)
WATTS_PER_PERCENT = 70.0

# Ścieżki – katalog projektu
PROJECT_ROOT = Path(__file__).resolve().parent
STATE_DIR = PROJECT_ROOT / "state"
LOG_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
TELEMETRY_DIR = DATA_DIR / "telemetry"

# Telemetria (JSONL)
TELEMETRY_ENABLED = _bool_env("TELEMETRY_ENABLED", True)
TELEMETRY_TZ = os.environ.get("TELEMETRY_TZ") or "Europe/Warsaw"

# Sterowanie inwerterem: domyślna wartość z env; plik override (jeśli istnieje) ma pierwszeństwo w runtime
GUARDIAN_CONTROL_ENABLED = _bool_env("GUARDIAN_CONTROL_ENABLED", True)
GUARDIAN_CONTROL_OVERRIDE_PATH = Path(
    os.environ.get("GUARDIAN_CONTROL_OVERRIDE_PATH")
    or (STATE_DIR / "guardian_control_override.json")
)

# Guardian egzekwuje rolling plan (target_net_kwh); plan jest liczony niezależnie od tego
PLANNER_EXECUTION_ENABLED = _bool_env("PLANNER_EXECUTION_ENABLED", False)
PLANNER_OVERRIDE_PATH = Path(
    os.environ.get("PLANNER_OVERRIDE_PATH")
    or (STATE_DIR / "planner_override.json")
)

# API dashboardu — pusty = endpointy /api/guardian/control wyłączone (503)
GUARDIAN_API_KEY = (os.environ.get("GUARDIAN_API_KEY") or "").strip()

# Proxy endpoints (lokalna sieć): RCE i PV forecast (Solcast proxy).
RCE_PROXY_BASE_URL = (os.environ.get("RCE_PROXY_BASE_URL") or "").strip().rstrip("/")
SOLCAST_PROXY_BASE_URL = (
    (os.environ.get("SOLCAST_PROXY_BASE_URL") or "").strip().rstrip("/")
)
PROXY_HTTP_TIMEOUT_S = _float_env("PROXY_HTTP_TIMEOUT_S", 10.0)

# OpenWeatherMap One Call 3.0 — korekta PV h+2…h+6 (puste key = wyłączone).
OPENWEATHER_API_KEY = (os.environ.get("OPENWEATHER_API_KEY") or "").strip()
_owm_lat_raw = (os.environ.get("OPENWEATHER_LAT") or "").strip()
_owm_lon_raw = (os.environ.get("OPENWEATHER_LON") or "").strip()
OPENWEATHER_LAT: float | None = float(_owm_lat_raw) if _owm_lat_raw else None
OPENWEATHER_LON: float | None = float(_owm_lon_raw) if _owm_lon_raw else None
OPENWEATHER_CACHE_TTL_S = _float_env("OPENWEATHER_CACHE_TTL_S", 900.0)
OPENWEATHER_HTTP_TIMEOUT_S = _float_env("OPENWEATHER_HTTP_TIMEOUT_S", 10.0)
PV_WEATHER_CORRECTION_ENABLED = _bool_env("PV_WEATHER_CORRECTION_ENABLED", True)

# Tesla Wall Connector Gen 3 — lokalne API /api/1/lifetime (puste = wyłączone)
TESLA_WC_HOST = (
    os.environ.get("TESLA_WC_HOST") or os.environ.get("TESLA_WC_IP") or ""
).strip()
TESLA_WC_TIMEOUT_S = _float_env("TESLA_WC_TIMEOUT_S", 5.0)
TESLA_WC_MAX_KW = _float_env("TESLA_WC_MAX_KW", 11.0)

EV_CHARGING_DECLARATION_PATH = Path(
    os.environ.get("EV_CHARGING_DECLARATION_PATH")
    or (STATE_DIR / "ev_charging_declaration.json")
)


def get_slot_id() -> str:
    """Zwraca eco_mode_1..4 dla slotu balansującego."""
    n = ECO_SLOT_BALANCING
    if not 1 <= n <= 4:
        raise ValueError(f"ECO_SLOT_BALANCING musi być 1..4, jest {n}")
    return f"eco_mode_{n}"
