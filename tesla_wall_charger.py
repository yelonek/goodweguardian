"""Odczyt licznika energii Tesla Wall Connector Gen 3 (lokalne API /api/1/lifetime)."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

import httpx
from pydantic import BaseModel, ConfigDict

from guardian_config import TELEMETRY_DIR, TESLA_WC_HOST, TESLA_WC_TIMEOUT_S

LIFETIME_PATH = "/api/1/lifetime"


class TwcLifetime(BaseModel):
    model_config = ConfigDict(extra="ignore")

    energy_wh: float


def twc_enabled() -> bool:
    return bool(TESLA_WC_HOST.strip())


def _lifetime_url(host: str) -> str:
    base = host.strip().rstrip("/")
    if not base.startswith("http://") and not base.startswith("https://"):
        base = f"http://{base}"
    return f"{base}{LIFETIME_PATH}"


def fetch_lifetime_energy_kwh(
    host: str | None = None,
    *,
    client: httpx.Client | None = None,
    timeout_s: float | None = None,
) -> float | None:
    """
    GET /api/1/lifetime → energy_wh przeliczone na kWh.

    Zwraca None gdy brak hosta, błąd HTTP lub niepoprawna odpowiedź.
    """
    h = (host or TESLA_WC_HOST).strip()
    if not h:
        return None

    url = _lifetime_url(h)
    timeout = float(TESLA_WC_TIMEOUT_S if timeout_s is None else timeout_s)
    log = logging.getLogger("guardian")
    close_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)

    try:
        resp = client.get(url)
        resp.raise_for_status()
        lifetime = TwcLifetime.model_validate_json(resp.content)
        if lifetime.energy_wh < 0:
            log.warning("Tesla WC lifetime: ujemne energy_wh=%s", lifetime.energy_wh)
            return None
        return lifetime.energy_wh / 1000.0
    except (httpx.HTTPError, ValueError, TypeError) as e:
        log.warning("Tesla WC lifetime read failed (%s): %s", h, e)
        return None
    finally:
        if close_client:
            client.close()


def hour_start_twc_kwh_from_telemetry(now: datetime) -> float | None:
    """Pierwszy E_twc_kwh w bieżącej lokalnej godzinie (z JSONL telemetrii)."""
    path = TELEMETRY_DIR / f"telemetry_{now.strftime('%Y-%m-%d')}.jsonl"
    if not path.exists():
        return None

    target_hour = now.hour
    best_minute: int | None = None
    start_kwh: float | None = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if int(row["local_hour"]) != target_hour:
                        continue
                    val = row.get("E_twc_kwh")
                    if val is None:
                        continue
                    minute = int(row["local_minute"])
                    kwh = float(val)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if best_minute is None or minute < best_minute:
                    best_minute = minute
                    start_kwh = kwh
    except OSError:
        return None
    return start_kwh


def compute_delta_twc_kwh(
    E_twc_kwh: float,
    *,
    hour_start_kwh: float,
) -> float:
    delta = E_twc_kwh - hour_start_kwh
    return max(0.0, delta)


def _first_twc_kwh_per_hour(local_date: date) -> dict[int, float]:
    """Najwcześniejsza próbka ``E_twc_kwh`` per godzina lokalna."""
    path = TELEMETRY_DIR / f"telemetry_{local_date.isoformat()}.jsonl"
    if not path.exists():
        return {}
    best: dict[int, tuple[int, float]] = {}
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    hour = int(row["local_hour"])
                    minute = int(row["local_minute"])
                    val = row.get("E_twc_kwh")
                    if val is None:
                        continue
                    v = float(val)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not (0 <= hour <= 23):
                    continue
                prev = best.get(hour)
                if prev is None or minute < prev[0]:
                    best[hour] = (minute, v)
    except OSError:
        return {}
    return {h: v for h, (_m, v) in best.items()}


def _max_delta_twc_per_hour(local_date: date) -> dict[int, float]:
    """Maksymalny ``delta_twc_kwh`` w każdej godzinie (fallback gdy brak granicy H+1)."""
    path = TELEMETRY_DIR / f"telemetry_{local_date.isoformat()}.jsonl"
    if not path.exists():
        return {}
    out: dict[int, float] = {}
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    hour = int(row["local_hour"])
                    val = row.get("delta_twc_kwh")
                    if val is None:
                        continue
                    v = max(0.0, float(val))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not (0 <= hour <= 23):
                    continue
                prev = out.get(hour)
                if prev is None or v > prev:
                    out[hour] = v
    except OSError:
        return {}
    return out


def _twc_sample_hours(local_date: date) -> set[int]:
    """Godziny z dowolną próbką TWC (``E_twc_kwh`` lub ``delta_twc_kwh``)."""
    path = TELEMETRY_DIR / f"telemetry_{local_date.isoformat()}.jsonl"
    if not path.exists():
        return set()
    hours: set[int] = set()
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    hour = int(row["local_hour"])
                    if row.get("E_twc_kwh") is None and row.get("delta_twc_kwh") is None:
                        continue
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if 0 <= hour <= 23:
                    hours.add(hour)
    except OSError:
        return set()
    return hours


def hourly_ev_kwh_from_telemetry(local_date: date) -> dict[int, float]:
    """
    kWh ładowania EV per godzina z telemetrii TWC.

    Preferuje Δ ``E_twc_kwh`` między pierwszą próbką H a H+1 (jak PV).
    Fallback: ``max(delta_twc_kwh)`` w godzinie. Godziny bez TWC — pominięte.
    """
    sample_hours = _twc_sample_hours(local_date)
    if not sample_hours:
        return {}

    first_today = _first_twc_kwh_per_hour(local_date)
    first_tomorrow = _first_twc_kwh_per_hour(local_date + timedelta(days=1))
    max_delta = _max_delta_twc_per_hour(local_date)

    out: dict[int, float] = {}
    for h in sorted(sample_hours):
        ev: float | None = None
        start = first_today.get(h)
        if start is not None:
            if h < 23:
                end = first_today.get(h + 1)
            else:
                end = first_tomorrow.get(0)
            if end is not None:
                delta = end - start
                if delta >= 0:
                    ev = delta
        if ev is None and h in max_delta:
            ev = max_delta[h]
        if ev is None and h in first_today:
            ev = 0.0
        if ev is not None:
            out[h] = ev
    return out


def main() -> None:
    """Jednorazowy odczyt lifetime (debug / smoke test)."""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    host = sys.argv[1] if len(sys.argv) > 1 else None
    kwh = fetch_lifetime_energy_kwh(host)
    if kwh is None:
        raise SystemExit(1)
    print(f"E_twc_kwh={kwh:.6f}")


if __name__ == "__main__":
    main()
