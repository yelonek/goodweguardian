"""Stan guardiana – pliki w katalogu state/.

- hourly_balance_YYYY-MM-DD.json: stan startu godziny (E_exp_start, E_imp_start)
- watchdog_state.json: lekki stan polityki watchdog (anti flip-flop, streak importu)
"""
import json
from datetime import datetime
from pathlib import Path

from guardian_config import STATE_DIR


def _state_path(date: datetime) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    d = date.strftime("%Y-%m-%d")
    return STATE_DIR / f"hourly_balance_{d}.json"


def load_state(now: datetime) -> tuple[float, float] | None:
    """
    Wczytuje zapis z początku bieżącej godziny.
    Zwraca (E_exp_start, E_imp_start) w kWh albo None, jeśli brak pliku / inna godzina.
    """
    path = _state_path(now)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    hour_key = now.strftime("%Y-%m-%dT%H")
    if data.get("hour") != hour_key:
        return None
    exp = data.get("E_exp_start")
    imp = data.get("E_imp_start")
    if exp is None or imp is None:
        return None
    return float(exp), float(imp)


def save_state(now: datetime, E_exp_start: float, E_imp_start: float) -> None:
    """Zapisuje stan na start bieżącej godziny (wywołanie przy minuty==0)."""
    path = _state_path(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    hour_key = now.strftime("%Y-%m-%dT%H")
    data = {
        "hour": hour_key,
        "E_exp_start": E_exp_start,
        "E_imp_start": E_imp_start,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _watchdog_path() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / "watchdog_state.json"


def load_watchdog_state() -> dict:
    """Wczytuje stan watchdog (bez wyjątków); zwraca dict z domyślnymi wartościami."""
    path = _watchdog_path()
    if not path.exists():
        return {
            "mode": "neutral",
            "mode_since": None,
            "import_streak": 0,
            "last_remaining_kwh": None,
            "soc_full_defense_carryover": False,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "mode": "neutral",
            "mode_since": None,
            "import_streak": 0,
            "last_remaining_kwh": None,
            "soc_full_defense_carryover": False,
        }
    if not isinstance(data, dict):
        return {
            "mode": "neutral",
            "mode_since": None,
            "import_streak": 0,
            "last_remaining_kwh": None,
            "soc_full_defense_carryover": False,
        }
    data.setdefault("mode", "neutral")
    data.setdefault("mode_since", None)
    data.setdefault("import_streak", 0)
    data.setdefault("last_remaining_kwh", None)
    data.setdefault("soc_full_defense_carryover", False)
    return data


def save_watchdog_state(state: dict) -> None:
    """Zapisuje stan watchdog."""
    path = _watchdog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
