"""Łączy godzinowe RCE (PSE) z taryfą G12 → szacunkowy koszt importu PLN/kWh."""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

import httpx

from guardian_config import DATA_DIR, TELEMETRY_TZ
from pse_rce import get_or_fetch_hourly_rce_pln_per_kwh
from tariff_g12 import G12TariffConfig, g12_tariff_from_env

PSE_RCE_BASE_URL = (
    os.environ.get("PSE_RCE_BASE_URL") or "https://api.raporty.pse.pl/api/rce-pln"
).rstrip("/")

PRICING_CACHE_DIR = Path(
    os.environ.get("PRICING_CACHE_DIR") or (DATA_DIR / "pricing")
)


def effective_import_pln_per_kwh(
    local_date: date,
    local_hour: int,
    *,
    tariff: G12TariffConfig | None = None,
    client: httpx.Client | None = None,
    force_refresh_rce: bool = False,
) -> float:
    """
    Koszt szacunkowy za 1 kWh importu z sieci w danej godzinie lokalnej (strefa TELEMETRY_TZ).

    RCE jest pobierane / czytane z cache dla dnia ``local_date``.
    """
    if not 0 <= local_hour <= 23:
        raise ValueError(f"local_hour musi być 0..23, jest {local_hour}")
    t = tariff if tariff is not None else g12_tariff_from_env()
    hourly_rce = get_or_fetch_hourly_rce_pln_per_kwh(
        local_date,
        base_url=PSE_RCE_BASE_URL,
        cache_dir=PRICING_CACHE_DIR,
        client=client,
        force_refresh=force_refresh_rce,
    )
    rce_kwh = hourly_rce[local_hour]
    return t.effective_import_pln_per_kwh(local_hour, rce_kwh)


def hourly_effective_import_pln_per_kwh_for_day(
    local_date: date,
    *,
    tariff: G12TariffConfig | None = None,
    client: httpx.Client | None = None,
    force_refresh_rce: bool = False,
) -> list[float]:
    """24 wartości PLN/kWh dla dnia ``local_date`` (indeks = godzina)."""
    t = tariff if tariff is not None else g12_tariff_from_env()
    hourly_rce = get_or_fetch_hourly_rce_pln_per_kwh(
        local_date,
        base_url=PSE_RCE_BASE_URL,
        cache_dir=PRICING_CACHE_DIR,
        client=client,
        force_refresh=force_refresh_rce,
    )
    return [t.effective_import_pln_per_kwh(h, hourly_rce[h]) for h in range(24)]


def pricing_timezone_name() -> str:
    """Nazwa strefy używana przy interpretacji godzin (spójnie z telemetrią)."""
    return TELEMETRY_TZ


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="24× effective import PLN/kWh (RCE + G12; strefy nocne na stało, dystrybucja z .env)"
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
