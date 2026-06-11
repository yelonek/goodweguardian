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

# Próg mocy bilansowania [kW] — wyświetlanie w logu dashboardu
BALANCE_POWER_THRESHOLD_KW = _float_env("BALANCE_POWER_THRESHOLD_KW", 0.3)

# ~70 W na 1% (plan)
WATTS_PER_PERCENT = 70.0

# Flappy Bird: bufor eksportu z nadwyżki PV, korekta deficytu, soak na koniec godziny
FLAPPY_BUFFER_DISCHARGE_PCT = _int_env("FLAPPY_BUFFER_DISCHARGE_PCT", 1)
# Soak ciągły (przy nadwyżce PV): deadband [SOAK_TARGET_KWH, SOAK_TRIGGER_KWH].
# > trigger → CHARGE w dół do target; w paśmie → hold; < target → drobny bufor (discharge 1%).
SOAK_TARGET_KWH = _float_env("SOAK_TARGET_KWH", 0.1)
SOAK_TRIGGER_KWH = _float_env("SOAK_TRIGGER_KWH", 0.2)
END_HOUR_WINDOW_S = _int_env("END_HOUR_WINDOW_S", 600)
END_HOUR_MAX_REMAINING_KWH = _float_env("END_HOUR_MAX_REMAINING_KWH", 0.2)
# Margines bezpieczeństwa: deficyt > max_recoverable × fraction → pełna moc do capu inwertera
RECOVERABLE_FRACTION = _float_env("RECOVERABLE_FRACTION", 0.9)
# Bias na lekki eksport przy korekcie deficytu [W]
GRID_EXPORT_BIAS_W = _float_env("GRID_EXPORT_BIAS_W", 150.0)
# Przy remaining<0 i brak mocy po obliczeniach: minimalne rozładowanie [%] (GoodWe: 0% bywa bezużyteczne). 0 = wyłącz.
WATCHDOG_MIN_DISCHARGE_ASSIST_PCT = _int_env("WATCHDOG_MIN_DISCHARGE_ASSIST_PCT", 1)

# SOC=100% “battery defense”: utrzymuj CHARGE 1% (blokuj discharge), dopóki bilans mocy nie jest „wystarczająco zły”.
# Wyjście z obrony pełnej sterowane jest progiem mocy bilansu (moc_bilans [kW]), nie energią godzinową.
SOC_FULL_DEFENSE_THRESHOLD_PCT = _float_env("SOC_FULL_DEFENSE_THRESHOLD_PCT", 99.5)
SOC_FULL_DEFENSE_CHARGE_PCT = _int_env("SOC_FULL_DEFENSE_CHARGE_PCT", -1)
# Próg mocy bilansu [kW], powyżej którego wyłączamy obronę pełną przy netto imporcie.
# Np. 0.5 = puść SOC defense, gdy potrzeba ≥0.5kW wyrównania (moc_bilans ~ -0.5kW).
SOC_FULL_DEFENSE_RELEASE_POWER_KW = _float_env("SOC_FULL_DEFENSE_RELEASE_POWER_KW", 0.5)
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


SOC_NIGHT_RESERVE_HOURS = _hours_csv_env("SOC_NIGHT_RESERVE_HOURS", "22,23,0,1,2,3,4,5")

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

# Runtime watchdog SOC (plik JSON); brak klucza = wartość z env jak przy starcie procesu.
GUARDIAN_WATCHDOG_OVERRIDE_PATH = Path(
    os.environ.get("GUARDIAN_WATCHDOG_OVERRIDE_PATH")
    or (STATE_DIR / "guardian_watchdog_override.json")
)

# API dashboardu — pusty = endpointy /api/guardian/control wyłączone (503)
GUARDIAN_API_KEY = (os.environ.get("GUARDIAN_API_KEY") or "").strip()

# Proxy endpoints (lokalna sieć): RCE i PV forecast (Solcast proxy).
RCE_PROXY_BASE_URL = (os.environ.get("RCE_PROXY_BASE_URL") or "").strip().rstrip("/")
# Mnożnik RCE z PSE/proxy → stawka rozliczenia eksportu u sprzedawcy (np. VAT 1,23).
RCE_EXPORT_MULTIPLIER = _float_env("RCE_EXPORT_MULTIPLIER", 1.23)
SOLCAST_PROXY_BASE_URL = (
    (os.environ.get("SOLCAST_PROXY_BASE_URL") or "").strip().rstrip("/")
)
PROXY_HTTP_TIMEOUT_S = _float_env("PROXY_HTTP_TIMEOUT_S", 10.0)

# Load forecast: korekta krótkoterminowa (średnia moc z ostatnich min vs baseline p50 bieżącej godz.)
LOAD_NOWCAST_ENABLED = _bool_env("LOAD_NOWCAST_ENABLED", True)
LOAD_NOWCAST_WINDOW_MIN = _int_env("LOAD_NOWCAST_WINDOW_MIN", 45)
LOAD_NOWCAST_DECAY_HOURS = _int_env("LOAD_NOWCAST_DECAY_HOURS", 4)
LOAD_NOWCAST_MAX_DELTA_KWH = _float_env("LOAD_NOWCAST_MAX_DELTA_KWH", 1.0)

# PV correction (k_intra): telemetria bieżącej godziny vs Solcast p50 na h i h+1
PV_CORRECTION_ENABLED = _bool_env("PV_CORRECTION_ENABLED", True)
PV_CORRECTION_EPS_KWH = _float_env("PV_CORRECTION_EPS_KWH", 0.1)
PV_CORRECTION_K_MIN = _float_env("PV_CORRECTION_K_MIN", 0.65)
PV_CORRECTION_K_MAX = _float_env("PV_CORRECTION_K_MAX", 1.35)


def get_slot_id() -> str:
    """Zwraca eco_mode_1..4 dla slotu balansującego."""
    n = ECO_SLOT_BALANCING
    if not 1 <= n <= 4:
        raise ValueError(f"ECO_SLOT_BALANCING musi być 1..4, jest {n}")
    return f"eco_mode_{n}"
