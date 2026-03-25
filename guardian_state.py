"""Stan godziny (E_exp_start, E_imp_start) – plik z datą w nazwie."""
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
