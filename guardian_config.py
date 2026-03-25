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

# Ścieżki – katalog projektu
PROJECT_ROOT = Path(__file__).resolve().parent
STATE_DIR = PROJECT_ROOT / "state"
LOG_DIR = PROJECT_ROOT / "logs"


def get_slot_id() -> str:
    """Zwraca eco_mode_1..4 dla slotu balansującego."""
    n = ECO_SLOT_BALANCING
    if not 1 <= n <= 4:
        raise ValueError(f"ECO_SLOT_BALANCING musi być 1..4, jest {n}")
    return f"eco_mode_{n}"
