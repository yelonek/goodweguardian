"""Pobieranie RCE (PLN/MWh) z publicznego API Raportów PSE — kwartały 15 min, agregacja do godziny."""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger("guardian")


class RceQuarter(BaseModel):
    """Jeden rekord z /api/rce-pln."""

    business_date: str
    period: str
    rce_pln: float = Field(description="RCE w PLN/MWh")


def parse_period_start_hour(period: str) -> int:
    """Godzina lokalna [0..23] z początku przedziału, np. '07:30 - 07:45' -> 7."""
    if " - " not in period:
        raise ValueError(f"nieoczekiwany format period: {period!r}")
    left = period.split(" - ", 1)[0].strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", left)
    if not m:
        raise ValueError(f"nie można sparsować godziny z period: {period!r}")
    h = int(m.group(1))
    if h == 24:
        return 0
    if not 0 <= h <= 23:
        raise ValueError(f"godzina poza 0..23 w period: {period!r}")
    return h


def aggregate_quarters_to_hourly_pln_per_kwh(quarters: list[RceQuarter]) -> list[float]:
    """
    Średnia z 4 kwartałów na godzinę lokalną -> PLN/kWh (RCE jest w PLN/MWh).
    Zwraca listę 24 wartości indeksów 0..23.
    """
    by_hour: dict[int, list[float]] = defaultdict(list)
    for q in quarters:
        h = parse_period_start_hour(q.period)
        by_hour[h].append(q.rce_pln)

    out: list[float] = []
    for h in range(24):
        vals = by_hour.get(h, [])
        if len(vals) != 4:
            raise ValueError(
                f"oczekiwano 4 kwartałów dla godziny {h}, jest {len(vals)}"
            )
        mean_mwh = sum(vals) / 4.0
        out.append(mean_mwh / 1000.0)
    return out


def _absolute_url(base: str, link: str | None) -> str | None:
    if not link:
        return None
    if urlparse(link).netloc:
        return link
    return urljoin(base.rstrip("/") + "/", link.lstrip("/"))


def fetch_rce_quarters_for_date(
    business_date: date,
    *,
    base_url: str,
    client: httpx.Client | None = None,
    timeout_s: float = 30.0,
) -> list[RceQuarter]:
    """
    Pobiera wszystkie kwartały dla business_date (OData $filter + ewentualna paginacja nextLink).
    """
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=timeout_s)
    assert client is not None
    try:
        d_str = business_date.isoformat()
        filter_expr = f"business_date eq '{d_str}'"
        base = base_url.rstrip("/")
        rows: list[dict[str, Any]] = []
        url: str | None = base
        first_page = True
        while url:
            if first_page:
                r = client.get(url, params={"$filter": filter_expr})
                first_page = False
            else:
                r = client.get(url)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"PSE API error: {data['error']}")
            chunk = data.get("value") if isinstance(data, dict) else None
            if not isinstance(chunk, list):
                raise RuntimeError(f"nieoczekiwany kształt odpowiedzi PSE: {type(data)}")
            rows.extend(chunk)
            raw_next = data.get("nextLink") if isinstance(data, dict) else None
            url = _absolute_url(base_url, raw_next) if raw_next else None

        quarters: list[RceQuarter] = []
        for row in rows:
            quarters.append(
                RceQuarter(
                    business_date=str(row["business_date"]),
                    period=str(row["period"]),
                    rce_pln=float(row["rce_pln"]),
                )
            )
        return quarters
    finally:
        if own_client:
            client.close()


def load_rce_cache(cache_path: Path) -> list[RceQuarter] | None:
    if not cache_path.is_file():
        return None
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("rce cache read failed %s: %s", cache_path, e)
        return None
    qraw = raw.get("quarters")
    if not isinstance(qraw, list):
        return None
    try:
        return [RceQuarter.model_validate(x) for x in qraw]
    except Exception:
        return None


def save_rce_cache(
    cache_path: Path,
    business_date: date,
    quarters: list[RceQuarter],
    hourly_pln_per_kwh: list[float],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "business_date": business_date.isoformat(),
        "quarters": [q.model_dump() for q in quarters],
        "hourly_rce_pln_per_kwh": hourly_pln_per_kwh,
    }
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_or_fetch_hourly_rce_pln_per_kwh(
    business_date: date,
    *,
    base_url: str,
    cache_dir: Path,
    client: httpx.Client | None = None,
    force_refresh: bool = False,
) -> list[float]:
    """
    Zwraca 24 wartości PLN/kWh (średnia RCE w godzinie).
    Używa pliku cache `rce_{date}.json` w cache_dir, chyba że force_refresh.
    """
    cache_path = cache_dir / f"rce_{business_date.isoformat()}.json"
    if not force_refresh:
        cached = load_rce_cache(cache_path)
        if cached is not None:
            try:
                return aggregate_quarters_to_hourly_pln_per_kwh(cached)
            except ValueError:
                log.warning("rce cache invalid aggregation, refetching: %s", cache_path)

    quarters = fetch_rce_quarters_for_date(business_date, base_url=base_url, client=client)
    hourly = aggregate_quarters_to_hourly_pln_per_kwh(quarters)
    save_rce_cache(cache_path, business_date, quarters, hourly)
    return hourly
