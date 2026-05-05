"""Konfiguracja guardiana (godzinowy balans) z .env."""

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

# Histereza [%] – domyślnie 15 i 2
HYSTERESIS_TOLERANCE_START = _float_env("HYSTERESIS_TOLERANCE_START", 15.0)
HYSTERESIS_TOLERANCE_END = _float_env("HYSTERESIS_TOLERANCE_END", 2.0)

# Próg mocy bilansowania [kW]
BALANCE_POWER_THRESHOLD_KW = _float_env("BALANCE_POWER_THRESHOLD_KW", 0.3)

# ~70 W na 1% (plan)
WATTS_PER_PERCENT = 70.0

# Watchdog policy (domyślnie: pozwól GoodWe działać, interweniuj późno / awaryjnie)
# Okno „domykania” na końcu godziny [s]
LATE_WINDOW_S = _int_env("LATE_WINDOW_S", 1200)
# Próg mocy wymaganej do domknięcia, powyżej którego zaczynamy interweniować w late window [kW]
WATCHDOG_LATE_POWER_THRESHOLD_KW = _float_env("WATCHDOG_LATE_POWER_THRESHOLD_KW", 0.45)
# Bias na lekki eksport (żeby unikać drogiego importu) [W]
GRID_EXPORT_BIAS_W = _float_env("GRID_EXPORT_BIAS_W", 150.0)
# Awaryjna interwencja, gdy utrwalony import poniżej progu [W] przez N cykli
WATCHDOG_IMPORT_W_THRESHOLD = _float_env("WATCHDOG_IMPORT_W_THRESHOLD", -300.0)
WATCHDOG_IMPORT_STREAK_MIN = _int_env("WATCHDOG_IMPORT_STREAK_MIN", 3)
# Minimalny czas trzymania kierunku (anti flip-flop) [s]
WATCHDOG_DWELL_S = _int_env("WATCHDOG_DWELL_S", 600)
# Awaryjnie: jeśli bilans energii jest już „nie do odrobienia” w samym late window,
# to interweniuj wcześniej (ułamek marginesu bezpieczeństwa).
WATCHDOG_UNRECOVERABLE_FRACTION = _float_env("WATCHDOG_UNRECOVERABLE_FRACTION", 0.9)
# Przy remaining<0 i 0% po clampie kierunku: minimalne rozładowanie [%] (GoodWe: 0% bywa bezużyteczne). 0 = wyłącz.
WATCHDOG_MIN_DISCHARGE_ASSIST_PCT = _int_env("WATCHDOG_MIN_DISCHARGE_ASSIST_PCT", 1)
# Charge w watchdogu tylko przy nadwyżce eksportu > tego progu [kWh] (0 = jak wcześniej poza blokadą przy 0).
WATCHDOG_CHARGE_MIN_REMAINING_KWH = _float_env("WATCHDOG_CHARGE_MIN_REMAINING_KWH", 0.05)
# Bufor eksportu: pierwsze N min godziny (0 = wył.), cel [kWh], minimalny +% rozładowania (jak min_discharge assist).
EXPORT_BUFFER_BUILD_MINUTES = _int_env("EXPORT_BUFFER_BUILD_MINUTES", 15)
EXPORT_BUFFER_TARGET_KWH = _float_env("EXPORT_BUFFER_TARGET_KWH", 0.1)
EXPORT_BUFFER_DISCHARGE_PCT = _int_env("EXPORT_BUFFER_DISCHARGE_PCT", 1)

# SOC=100% “battery defense”: utrzymuj CHARGE 1% (blokuj discharge) dopóki bilans nie jest „wystarczająco zły”.
# Early window może tolerować mały import (ujemny próg), late window zawsze domyka bilans do 0.
SOC_FULL_DEFENSE_THRESHOLD_PCT = _float_env("SOC_FULL_DEFENSE_THRESHOLD_PCT", 99.5)
SOC_FULL_DEFENSE_CHARGE_PCT = _int_env("SOC_FULL_DEFENSE_CHARGE_PCT", -1)
# Early: 0 = puść obronę przy pierwszym imporcie netto w godzinie (remaining_kwh ≤ 0).
# Ujemna = budżet importu [kWh] zanim puścisz obronę (np. -0.3 → puść dopiero przy remaining ≤ -0.3).
SOC_FULL_DEFENSE_EARLY_RELEASE_KWH = _float_env(
    "SOC_FULL_DEFENSE_EARLY_RELEASE_KWH", -0.3
)
# Pierwsze N minut nowej godziny: tarcza SOC jak po aktywności w ostatnich N minutach poprzedniej godziny.
SOC_FULL_DEFENSE_CARRYOVER_MINUTES = _int_env("SOC_FULL_DEFENSE_CARRYOVER_MINUTES", 5)

# Maksymalna długość pojedynczego okna zapisu ecoslota [min]
WATCHDOG_MAX_SLOT_MIN = _int_env("WATCHDOG_MAX_SLOT_MIN", 5)
# Maksymalna długość pojedynczego okna dla SOC-full defense [min]
SOC_FULL_DEFENSE_MAX_SLOT_MIN = _int_env("SOC_FULL_DEFENSE_MAX_SLOT_MIN", 15)

# SOC niski: ogranicz discharge do średniego zużycia domu z ostatnich N minut.
# To nie jest nocna rezerwa SOC: działa niezależnie od godziny i bilansu, żeby skoki obciążenia szły z sieci.
SOC_LOW_DEFENSE_THRESHOLD_PCT = _float_env("SOC_LOW_DEFENSE_THRESHOLD_PCT", 22.0)
# Okno średniej kroczącej zużycia domu [min]. 0 = wyłącz nowy limit i użyj legacy CHARGE poniżej.
SOC_LOW_DISCHARGE_AVG_MINUTES = _int_env("SOC_LOW_DISCHARGE_AVG_MINUTES", 60)
# Gdy brak historii telemetrii, użyj spokojnego fallbacku [W]. 0 = brak fallbacku.
SOC_LOW_DISCHARGE_FALLBACK_W = _float_env("SOC_LOW_DISCHARGE_FALLBACK_W", 300.0)
# Opcjonalny sufit limitu discharge [W]. 0 = bez dodatkowego sufitu poza P_BATTERY.
SOC_LOW_DISCHARGE_MAX_W = _float_env("SOC_LOW_DISCHARGE_MAX_W", 0.0)
# Legacy fallback: CHARGE 1% (blok rozładowania) dopóki remaining_kwh > celu godziny.
SOC_LOW_DEFENSE_CHARGE_PCT = _int_env("SOC_LOW_DEFENSE_CHARGE_PCT", -1)
SOC_LOW_DEFENSE_RELEASE_REMAINING_KWH = _float_env(
    "SOC_LOW_DEFENSE_RELEASE_REMAINING_KWH", 0.0
)

# Nocna rezerwa SOC: w godzinach nocnych blokuj discharge gdy SOC ≤ progu — by zostawić
# zapas na poranne drogie godziny (po 6:00, zanim wstanie słońce). 0 = wyłączone.
SOC_NIGHT_RESERVE_PCT = _float_env("SOC_NIGHT_RESERVE_PCT", 0.0)
SOC_NIGHT_RESERVE_CHARGE_PCT = _int_env("SOC_NIGHT_RESERVE_CHARGE_PCT", -1)


def _hours_csv_env(name: str, default: str) -> frozenset[int]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        raw = default
    hours: set[int] = set()
    for part in raw.split(","):
        s = part.strip()
        if not s:
            continue
        h = int(s)
        if not 0 <= h <= 23:
            raise ValueError(f"{name}: godzina poza zakresem 0..23: {h}")
        hours.add(h)
    return frozenset(hours)


SOC_NIGHT_RESERVE_HOURS = _hours_csv_env(
    "SOC_NIGHT_RESERVE_HOURS", "22,23,0,1,2,3,4,5"
)

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

# API dashboardu — pusty = endpointy /api/guardian/control wyłączone (503)
GUARDIAN_API_KEY = (os.environ.get("GUARDIAN_API_KEY") or "").strip()

# Proxy endpoints (lokalna sieć): RCE i PV forecast (Solcast proxy).
RCE_PROXY_BASE_URL = (os.environ.get("RCE_PROXY_BASE_URL") or "").strip().rstrip("/")
SOLCAST_PROXY_BASE_URL = (os.environ.get("SOLCAST_PROXY_BASE_URL") or "").strip().rstrip("/")
PROXY_HTTP_TIMEOUT_S = _float_env("PROXY_HTTP_TIMEOUT_S", 10.0)


def get_slot_id() -> str:
    """Zwraca eco_mode_1..4 dla slotu balansującego."""
    n = ECO_SLOT_BALANCING
    if not 1 <= n <= 4:
        raise ValueError(f"ECO_SLOT_BALANCING musi być 1..4, jest {n}")
    return f"eco_mode_{n}"
