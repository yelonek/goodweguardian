"""Odczyt licznika energii Tesla Wall Connector Gen 3 (lokalne API /api/1/lifetime)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import httpx
from pydantic import BaseModel, ConfigDict

from guardian_config import TELEMETRY_DIR, TESLA_WC_HOST, TESLA_WC_TIMEOUT_S
from telemetry_scan import DayScan, scan_telemetry_file

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


def _scan_local_date(local_date: date) -> DayScan | None:
    path = TELEMETRY_DIR / f"telemetry_{local_date.isoformat()}.jsonl"
    return scan_telemetry_file(path)


def hour_start_twc_kwh_from_telemetry(now: datetime) -> float | None:
    """Pierwszy E_twc_kwh w bieżącej lokalnej godzinie (z JSONL telemetrii)."""
    scan = _scan_local_date(now.date())
    if scan is None:
        return None
    return scan.first_twc.get(now.hour)


def compute_delta_twc_kwh(
    E_twc_kwh: float,
    *,
    hour_start_kwh: float,
) -> float:
    delta = E_twc_kwh - hour_start_kwh
    return max(0.0, delta)


def hourly_ev_kwh_from_telemetry(local_date: date) -> dict[int, float]:
    """
    kWh ładowania EV per godzina z telemetrii TWC.

    Preferuje Δ ``E_twc_kwh`` między pierwszą próbką H a H+1 (jak PV).
    Fallback: ``max(delta_twc_kwh)`` w godzinie. Godziny bez TWC — pominięte.
    """
    today = _scan_local_date(local_date)
    if today is None or not today.twc_hours:
        return {}
    tomorrow = _scan_local_date(local_date + timedelta(days=1))
    sample_hours = today.twc_hours
    first_today = today.first_twc
    first_tomorrow = tomorrow.first_twc if tomorrow is not None else {}
    max_delta = today.max_delta_twc

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
