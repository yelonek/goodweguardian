"""Testy PSE RCE: parsowanie period, agregacja godzinowa, fetch z mockiem HTTP."""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx
import pytest

from pse_rce import (
    RceQuarter,
    aggregate_quarters_to_hourly_pln_per_kwh,
    fetch_rce_quarters_for_date,
    get_or_fetch_hourly_rce_pln_per_kwh,
    parse_period_start_hour,
)


def test_parse_period_start_hour() -> None:
    assert parse_period_start_hour("07:30 - 07:45") == 7
    assert parse_period_start_hour("00:00 - 00:15") == 0
    assert parse_period_start_hour("23:45 - 24:00") == 23


def _period_for_quarter(h: int, q: int) -> str:
    starts = [f"{h:02d}:00", f"{h:02d}:15", f"{h:02d}:30", f"{h:02d}:45"]
    if q == 0:
        end = f"{h:02d}:15"
    elif q == 1:
        end = f"{h:02d}:30"
    elif q == 2:
        end = f"{h:02d}:45"
    else:
        end = "24:00" if h == 23 else f"{h + 1:02d}:00"
    return f"{starts[q]} - {end}"


def _full_day_quarters(
    business_date: str, rce_fn: Callable[[int, int], float] | None = None
) -> list[RceQuarter]:
    out: list[RceQuarter] = []
    for h in range(24):
        for q in range(4):
            rce = 400.0 if rce_fn is None else rce_fn(h, q)
            out.append(
                RceQuarter(
                    business_date=business_date,
                    period=_period_for_quarter(h, q),
                    rce_pln=rce,
                )
            )
    return out


def test_aggregate_uniform() -> None:
    qs = _full_day_quarters("2026-04-01", lambda h, q: 1000.0)
    hourly = aggregate_quarters_to_hourly_pln_per_kwh(qs)
    assert len(hourly) == 24
    assert all(abs(x - 1.0) < 1e-9 for x in hourly)


def test_aggregate_mixed_hour() -> None:
    def rce(h: int, q: int) -> float:
        if h == 10:
            return [0.0, 1000.0, 2000.0, 3000.0][q]
        return 400.0

    qs = _full_day_quarters("2026-04-01", rce)
    hourly = aggregate_quarters_to_hourly_pln_per_kwh(qs)
    assert hourly[10] == pytest.approx(1500.0 / 1000.0)


def test_aggregate_wrong_count_raises() -> None:
    qs = _full_day_quarters("2026-04-01")[:90]
    with pytest.raises(ValueError, match="oczekiwano 4 kwartałów"):
        aggregate_quarters_to_hourly_pln_per_kwh(qs)


def test_fetch_rce_merges_pages() -> None:
    row = {
        "business_date": "2026-04-01",
        "period": "00:00 - 00:15",
        "rce_pln": 100.0,
    }
    page1 = {
        "value": [row],
        "nextLink": "https://pse.test/api/rce-pln?$after=abc",
    }
    page2 = {"value": [dict(row, period="00:15 - 00:30", rce_pln=200.0)]}

    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "$after" not in u:
            return httpx.Response(200, json=page1)
        return httpx.Response(200, json=page2)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        got = fetch_rce_quarters_for_date(
            date(2026, 4, 1), base_url="https://pse.test/api/rce-pln", client=client
        )
    assert len(got) == 2
    assert got[0].rce_pln == 100.0


def test_get_or_fetch_uses_cache(tmp_path: Path) -> None:
    qs = _full_day_quarters("2026-04-01", lambda h, q: 800.0)
    hourly = aggregate_quarters_to_hourly_pln_per_kwh(qs)
    cache = tmp_path / "rce_2026-04-01.json"
    cache.write_text(
        json.dumps(
            {
                "business_date": "2026-04-01",
                "quarters": [q.model_dump() for q in qs],
                "hourly_rce_pln_per_kwh": hourly,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    calls: list[str] = []

    def boom(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("HTTP should not be used when cache hits")

    transport = httpx.MockTransport(boom)
    with httpx.Client(transport=transport) as client:
        out = get_or_fetch_hourly_rce_pln_per_kwh(
            date(2026, 4, 1),
            base_url="https://pse.test/api/rce-pln",
            cache_dir=tmp_path,
            client=client,
            force_refresh=False,
        )
    assert not calls
    assert len(out) == 24
    assert out[0] == pytest.approx(0.8)
