"""RCE godzinowe + taryfa G12: import (dystrybucja+energia wg strefy), eksport wg RCE osobno."""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from guardian_config import DATA_DIR, RCE_PROXY_BASE_URL, TELEMETRY_TZ
from pse_rce import get_or_fetch_hourly_rce_pln_per_kwh
from tariff_g12 import G12TariffConfig, g12_tariff_from_env

PSE_RCE_BASE_URL = (
    os.environ.get("PSE_RCE_BASE_URL") or "https://api.raporty.pse.pl/api/rce-pln"
).rstrip("/")

PRICING_CACHE_DIR = Path(
    os.environ.get("PRICING_CACHE_DIR") or (DATA_DIR / "pricing")
)
log = logging.getLogger("guardian")


def _hour_from_iso(ts: str) -> int | None:
    try:
        hh = ts.split("T", 1)[1].split(":", 1)[0]
        h = int(hh)
    except (IndexError, ValueError):
        return None
    if 0 <= h <= 23:
        return h
    return None


def fetch_hourly_rce_from_proxy(
    local_date: date,
    *,
    base_url: str,
    client: httpx.Client | None = None,
) -> list[float]:
    """
    Pobiera 24 ceny RCE [PLN/kWh] z proxy endpointu /api/rce?date=YYYY-MM-DD.
    """
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=20.0)
    assert client is not None
    try:
        r = client.get(f"{base_url.rstrip('/')}/api/rce", params={"date": local_date.isoformat()})
        r.raise_for_status()
        payload = r.json()
    finally:
        if own_client:
            client.close()

    hours = payload.get("hours")
    if not isinstance(hours, list):
        raise ValueError("RCE proxy response missing 'hours' list")
    out: list[float | None] = [None] * 24
    for item in hours:
        if not isinstance(item, dict):
            continue
        hour_start = str(item.get("hour_start") or "")
        price = item.get("price_pln_kwh")
        hour = _hour_from_iso(hour_start)
        if hour is None:
            continue
        try:
            out[hour] = float(price)
        except (TypeError, ValueError):
            continue
    if any(v is None for v in out):
        missing = [i for i, v in enumerate(out) if v is None]
        raise ValueError(f"RCE proxy missing hours: {missing}")
    return [float(v) for v in out]


def get_hourly_rce_pln_per_kwh(
    local_date: date,
    *,
    client: httpx.Client | None = None,
    force_refresh_rce: bool = False,
) -> tuple[list[float], str]:
    """
    Zwraca 24 wartości RCE [PLN/kWh] i źródło danych.
    """
    if RCE_PROXY_BASE_URL:
        try:
            return (
                fetch_hourly_rce_from_proxy(local_date, base_url=RCE_PROXY_BASE_URL, client=client),
                "rce_proxy",
            )
        except Exception as e:
            log.warning("rce proxy fetch failed for %s: %s", local_date.isoformat(), e)
    hourly_rce = get_or_fetch_hourly_rce_pln_per_kwh(
        local_date,
        base_url=PSE_RCE_BASE_URL,
        cache_dir=PRICING_CACHE_DIR,
        client=client,
        force_refresh=force_refresh_rce,
    )
    return hourly_rce, "pse_api"


def effective_import_pln_per_kwh(
    local_date: date,
    local_hour: int,
    *,
    tariff: G12TariffConfig | None = None,
    client: httpx.Client | None = None,
    force_refresh_rce: bool = False,
) -> float:
    """
    Koszt 1 kWh importu netto w danej godzinie lokalnej: dystrybucja + stała energia (strefa G12).

    ``local_date`` / ``client`` / ``force_refresh_rce`` są ignorowane (sygnatura zostaje dla zgodności).
    RCE nie wchodzi w cenę importu — przy eksporcie netto stosuje się ``rce_pln_kwh`` z breakdownu.
    """
    _ = (local_date, client, force_refresh_rce)
    if not 0 <= local_hour <= 23:
        raise ValueError(f"local_hour musi być 0..23, jest {local_hour}")
    t = tariff if tariff is not None else g12_tariff_from_env()
    return t.import_pln_per_kwh(local_hour)


def hourly_effective_import_pln_per_kwh_for_day(
    local_date: date,
    *,
    tariff: G12TariffConfig | None = None,
    client: httpx.Client | None = None,
    force_refresh_rce: bool = False,
) -> list[float]:
    """24 wartości PLN/kWh importu (tylko taryfa G12) dla dnia — indeks = godzina."""
    _ = (local_date, client, force_refresh_rce)
    t = tariff if tariff is not None else g12_tariff_from_env()
    return [t.import_pln_per_kwh(h) for h in range(24)]


def pricing_day_breakdown(
    local_date: date,
    *,
    tariff: G12TariffConfig | None = None,
    client: httpx.Client | None = None,
    force_refresh_rce: bool = False,
) -> dict[str, Any]:
    """
    Rozkład dla dnia: RCE (eksport) + import PLN/kWh (dystrybucja + energia stała wg strefy G12).
    """
    t = tariff if tariff is not None else g12_tariff_from_env()
    hourly_rce, source = get_hourly_rce_pln_per_kwh(
        local_date,
        client=client,
        force_refresh_rce=force_refresh_rce,
    )
    hours: list[dict[str, Any]] = []
    for h in range(24):
        zone = t.zone_for_hour(h)
        imp = t.import_pln_per_kwh(h)
        hours.append(
            {
                "hour": h,
                "zone": zone,
                "rce_pln_kwh": hourly_rce[h],
                "import_pln_per_kwh": imp,
                "effective_import_pln_kwh": imp,
            }
        )
    return {
        "date": local_date.isoformat(),
        "source": source,
        "timezone": pricing_timezone_name(),
        "hours": hours,
    }


def pricing_timezone_name() -> str:
    """Nazwa strefy używana przy interpretacji godzin (spójnie z telemetrią)."""
    return TELEMETRY_TZ


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="24× import PLN/kWh (dystrybucja+energia G12 wg strefy); RCE nie wchodzi w import"
    )
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        required=True,
        help="YYYY-MM-DD (business_date PSE = dzień dostawy, lokalnie PL)",
    )
    parser.add_argument(
        "--refresh-rce",
        action="store_true",
        help="wymuś ponowne pobranie RCE (pomiń cache)",
    )
    args = parser.parse_args()
    tz = pricing_timezone_name()
    row = hourly_effective_import_pln_per_kwh_for_day(
        args.date, force_refresh_rce=args.refresh_rce
    )
    print(f"timezone={tz} date={args.date.isoformat()} PLN/kWh per local hour")
    for h, v in enumerate(row):
        print(f"{h:02d}:00  {v:.6f}")
