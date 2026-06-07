from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

import guardian_config as guardian_cfg

from energy_pricing import pricing_day_breakdown
from guardian_config import LOG_DIR, TELEMETRY_DIR, TELEMETRY_TZ
from ecoslot_service import (
    balancing_slot_id,
    editable_slot_ids,
    fetch_ecoslots_payload,
    load_ecoslots_payload_from_snapshot,
    write_ecoslot,
)
from guardian_control import effective_control_enabled, write_control_override
from planner_control import (
    effective_planner_execution_enabled,
    write_planner_execution_override,
)
from guardian_watchdog_override import (
    apply_watchdog_override_updates,
    clear_watchdog_override,
    watchdog_soc_api_payload,
)
from baseline_info import baseline_spec
from load_forecast import (
    build_daily_hourly_kwh_cache,
    forecast_load_hours,
    predict_load_one_hour,
    run_load_forecast_backtest,
)
from pv_forecast import fetch_hourly_pv_forecast, fetch_hourly_pv_forecast_with_history
from planner.plan_store import load_latest_plan, load_plan
from planner.telemetry import hourly_actuals

app = FastAPI(title="GoodWeGuardian Dashboard", version="0.1.0")

logger = logging.getLogger(__name__)

LOG_PATH = Path(os.environ.get("GUARDIAN_LOG_PATH") or (LOG_DIR / "guardian.log"))

_HEAVY_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dash-heavy")
_forecast_build_lock = threading.Lock()


async def _run_heavy(fn, /, *args, **kwargs):
    """Sync CPU/IO work off the asyncio event loop (single uvicorn worker)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _HEAVY_EXECUTOR, lambda: fn(*args, **kwargs)
    )


class GuardianControlBody(BaseModel):
    control_enabled: bool


class PlannerControlBody(BaseModel):
    planner_execution_enabled: bool


class WatchdogSocUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    soc_night_reserve_pct: float | None = Field(default=None, ge=0, le=100)
    soc_night_reserve_charge_pct: int | None = Field(default=None, ge=-1, le=100)
    soc_night_reserve_hours: list[int] | None = None
    soc_low_defense_threshold_pct: float | None = Field(default=None, ge=0, le=100)
    soc_full_defense_threshold_pct: float | None = Field(default=None, ge=0, le=100)

    @field_validator("soc_night_reserve_hours")
    @classmethod
    def _hours(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return None
        if len(v) == 0:
            raise ValueError("soc_night_reserve_hours: use null to clear, not []")
        out: set[int] = set()
        for h in v:
            hi = int(h)
            if not 0 <= hi <= 23:
                raise ValueError("soc_night_reserve_hours: hour out of 0..23")
            out.add(hi)
        return sorted(out)


class EcoslotWriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_h: int = Field(ge=0, le=23)
    start_m: int = Field(ge=0, le=59)
    end_h: int = Field(ge=0, le=23)
    end_m: int = Field(ge=0, le=59)
    power: int = Field(ge=-100, le=100)
    days: str | list[int] = "Mon-Sun"
    soc: int = Field(default=100, ge=10, le=100)
    months: str | list[int] | None = None
    enabled: bool = True


def _require_guardian_api_key(
    x_guardian_api_key: str | None = Header(default=None),
) -> None:
    expected = guardian_cfg.GUARDIAN_API_KEY
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="GUARDIAN_API_KEY is not set; control API disabled",
        )
    if x_guardian_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@dataclass(frozen=True)
class DashboardRow:
    ts: datetime | None
    raw: str
    fields: dict[str, Any]


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[INFO\] dashboard \| ")


def _parse_float(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int(s: str) -> int | None:
    try:
        return int(s)
    except ValueError:
        return None


def _tail_lines(path: Path, max_lines: int, max_bytes: int = 512_000) -> list[str]:
    if max_lines <= 0:
        return []
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            end = f.tell()
            read = min(end, max_bytes)
            f.seek(end - read)
            data = f.read(read)
    except OSError:
        return []

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-max_lines:]


def _parse_dashboard_line(line: str) -> DashboardRow | None:
    m = _TS_RE.match(line)
    if not m:
        return None
    ts_s = m.group(1)
    try:
        ts = datetime.strptime(ts_s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        ts = None

    # Best-effort extraction. This is intentionally permissive (log format evolves).
    fields: dict[str, Any] = {"ts": ts_s}

    def grab(pattern: str, cast) -> Any:
        mm = re.search(pattern, line)
        if not mm:
            return None
        return cast(mm.group(1))

    fields["remaining_kwh"] = grab(r"balans_godz=([+-]?\d+\.\d+)\s+kWh", _parse_float)
    fields["balancing_kw"] = grab(r"moc_bilans=([+-]?\d+\.\d+)\s+kW", _parse_float)
    fields["grid_kw"] = grab(r"sieć=([+-]?\d+\.\d+)\s+kW", _parse_float)
    fields["pv_kw"] = grab(r"PV=(\d+\.\d+)\s+kW", _parse_float)
    fields["house_w"] = grab(r"dom=(\d+)\s+W", _parse_int)
    fields["soc_pct"] = grab(r"SOC=(\d+)%", _parse_int)
    fields["p_bat_w"] = grab(r"P_bat=([+-]?\d+)\s+W", _parse_int)
    fields["time_to_end_s"] = grab(r"do_końca=(\d+)s", _parse_int)

    fields["slot_bal"] = grab(r"slot_bal=(True|False)", lambda v: v == "True")
    fields["other_eco"] = grab(r"inny_eco=(True|False)", lambda v: v == "True")
    fields["ecoslot_read_pct"] = grab(r"ecoslot_read%=?([+-]?\d+)", _parse_int)
    fields["threshold_kw"] = grab(r"próg=(\d+\.\d+)\s+kW", _parse_float)
    fields["intervene"] = grab(r"interwen=(True|False)", lambda v: v == "True")

    # reason is between "| interwen=... | " and optional "| cmd="
    mm_reason = re.search(
        r"\|\s+interwen=(?:True|False)\s+\|\s+([^|]+?)(?:\s+\|\s+cmd=|$)", line
    )
    fields["reason"] = mm_reason.group(1).strip() if mm_reason else None

    mm_cmd = re.search(r"\|\s+cmd=(On|Off)\s+([+-]?\d+)%\s+(\d+)s", line)
    if mm_cmd:
        fields["cmd_enabled"] = mm_cmd.group(1) == "On"
        fields["cmd_pct"] = _parse_int(mm_cmd.group(2))
        fields["cmd_duration_s"] = _parse_int(mm_cmd.group(3))
    else:
        fields["cmd_enabled"] = None
        fields["cmd_pct"] = None
        fields["cmd_duration_s"] = None

    return DashboardRow(ts=ts, raw=line, fields=fields)


def read_history(limit: int) -> list[DashboardRow]:
    lines = _tail_lines(LOG_PATH, max_lines=max(2000, limit * 3))
    rows: list[DashboardRow] = []
    for line in reversed(lines):
        r = _parse_dashboard_line(line)
        if r:
            rows.append(r)
            if len(rows) >= limit:
                break
    return rows  # newest first


def annotate_history_closing_balance(rows: list[DashboardRow]) -> None:
    """Dla wierszy o pełnej godzinie (minute==0) dolicza bilans KOŃCOWY poprzedniej
    godziny — bo runner resetuje ``remaining_kwh`` na :00, więc live byłoby 0.

    Bilans liczony jest dokładnie z liczników telemetrii ``E_imp_kwh/E_exp_kwh``
    (ta sama metoda co KPI: pierwszy odczyt godziny H vs pierwszy odczyt H+1,
    ``net = Δexp − Δimp``), więc obejmuje całą godzinę, łącznie z ostatnią minutą.
    Wynik trafia do ``fields['closing_prev_hour_kwh']``; ``remaining_kwh`` pozostaje
    nietknięty (Current state ma pokazywać live).

    rows: newest-first (jak z :func:`read_history`).
    """
    targets: list[tuple[int, date, int]] = []
    needed_days: set[date] = set()
    for i, r in enumerate(rows):
        r.fields["closing_prev_hour_kwh"] = None
        ts = r.ts
        if ts is None or ts.minute != 0:
            continue
        prev_dt = ts - timedelta(hours=1)  # godzina, która właśnie się zakończyła
        targets.append((i, prev_dt.date(), prev_dt.hour))
        needed_days.add(prev_dt.date())

    if not targets:
        return

    net_by_day: dict[date, dict[int, dict[str, Any]]] = {}
    for d in needed_days:
        nets, _warnings = _hourly_counter_net_kwh(day=d)
        net_by_day[d] = nets

    for i, prev_date, prev_hour in targets:
        hn = net_by_day.get(prev_date, {}).get(prev_hour, {})
        if hn.get("complete") and hn.get("net_kwh") is not None:
            rows[i].fields["closing_prev_hour_kwh"] = hn["net_kwh"]


def _read_telemetry_day(local_date: date) -> list[dict[str, Any]]:
    path = TELEMETRY_DIR / f"telemetry_{local_date.isoformat()}.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    rows.append(data)
    except OSError:
        return []
    return rows


def _parse_ts_utc(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt


def _epoch_sort_key(r: dict[str, Any]) -> float:
    dt = _parse_ts_utc(str(r.get("ts_utc", "")))
    if dt is None:
        return float("-inf")
    return dt.timestamp()


def _telemetry_sorted_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict):
                        rows.append(data)
        except OSError:
            continue
    rows.sort(key=_epoch_sort_key)
    return rows


def _first_reading_in_hour(
    rows: list[dict[str, Any]], *, local_date: date, hour: int
) -> dict[str, Any] | None:
    """Pierwszy rekord telemetrii w danej lokalnej godzinie (granica „po pełnej godzinie”)."""
    d_s = local_date.isoformat()
    for r in rows:
        try:
            if str(r.get("local_date")) != d_s:
                continue
            if int(r.get("local_hour")) != hour:
                continue
            return r
        except (TypeError, ValueError):
            continue
    return None


def _hourly_counter_net_kwh(
    *, day: date
) -> tuple[dict[int, dict[str, Any]], list[str]]:
    """
    Dla każdej godziny H: bilans energii między granicami pełnych godzin [H, H+1),
    na licznikach E_imp_kwh / E_exp_kwh:
      Δimp = E_imp(H+1 pierwszy) − E_imp(H pierwszy)
      Δexp = E_exp(H+1 pierwszy) − E_exp(H pierwszy)
      net_kWh = Δexp − Δimp  (jak remaining_kwh w runnerze)

    Do godziny 23 potrzebny jest pierwszy pomiar w 00:00 następnego dnia — dokładamy plik telemetry_{D+1}.
    """
    warnings: list[str] = []
    next_day = day + timedelta(days=1)
    rows_full = _telemetry_sorted_rows(
        [
            TELEMETRY_DIR / f"telemetry_{day.isoformat()}.jsonl",
            TELEMETRY_DIR / f"telemetry_{next_day.isoformat()}.jsonl",
        ]
    )
    out: dict[int, dict[str, Any]] = {}
    for h in range(24):
        start = _first_reading_in_hour(rows_full, local_date=day, hour=h)
        end_hour = 0 if h == 23 else h + 1
        end_date = next_day if h == 23 else day
        end = _first_reading_in_hour(rows_full, local_date=end_date, hour=end_hour)
        if start is None or end is None:
            out[h] = {
                "net_kwh": None,
                "delta_imp_kwh": None,
                "delta_exp_kwh": None,
                "complete": False,
            }
            if start is None:
                warnings.append(f"Brak pierwszego pomiaru w godzinie {h:02d}")
            if end is None:
                warnings.append(
                    f"Brak pierwszego pomiaru po godzinie {h:02d} (koniec interwału)"
                )
            continue
        try:
            imp0 = float(start["E_imp_kwh"])
            exp0 = float(start["E_exp_kwh"])
            imp1 = float(end["E_imp_kwh"])
            exp1 = float(end["E_exp_kwh"])
        except (KeyError, TypeError, ValueError):
            out[h] = {
                "net_kwh": None,
                "delta_imp_kwh": None,
                "delta_exp_kwh": None,
                "complete": False,
            }
            warnings.append(f"Błędne liczniki w godzinie {h:02d}")
            continue
        d_imp = imp1 - imp0
        d_exp = exp1 - exp0
        out[h] = {
            "net_kwh": d_exp - d_imp,
            "delta_imp_kwh": d_imp,
            "delta_exp_kwh": d_exp,
            "complete": True,
        }
    return out, warnings


def _kpi_for_day(local_date: date) -> dict[str, Any]:
    rows = _read_telemetry_day(local_date)
    pricing = pricing_day_breakdown(local_date)
    price_by_hour = {int(h["hour"]): h for h in pricing["hours"]}
    hourly_net, boundary_warnings = _hourly_counter_net_kwh(day=local_date)

    hours: list[dict[str, Any]] = []
    deposit_day_pln = 0.0
    bill_day_pln = 0.0
    net_export_kwh_pos = 0.0
    net_import_kwh_pos = 0.0

    for hour in range(24):
        p = price_by_hour.get(hour, {})
        rce = float(p.get("rce_pln_kwh", 0.0))
        eff = float(p.get("import_pln_per_kwh", p.get("effective_import_pln_kwh", 0.0)))
        hn = hourly_net.get(hour, {})
        net_kwh = hn.get("net_kwh")
        complete = bool(hn.get("complete"))
        deposit_h = None
        bill_h = None
        if complete and net_kwh is not None:
            if net_kwh > 0:
                surplus_kwh = net_kwh
                deposit_h = surplus_kwh * rce
                deposit_day_pln += deposit_h
                net_export_kwh_pos += surplus_kwh
            elif net_kwh < 0:
                deficit_kwh = -net_kwh
                bill_h = deficit_kwh * eff
                bill_day_pln += bill_h
                net_import_kwh_pos += deficit_kwh
        hours.append(
            {
                "hour": hour,
                "net_kwh": net_kwh,
                "delta_imp_kwh": hn.get("delta_imp_kwh"),
                "delta_exp_kwh": hn.get("delta_exp_kwh"),
                "interval_complete": complete,
                "rce_pln_kwh": rce,
                "effective_import_pln_kwh": eff,
                "import_pln_per_kwh": eff,
                "deposit_add_pln": deposit_h,
                "electricity_bill_add_pln": bill_h,
            }
        )

    totals: dict[str, Any] = {
        "deposit_add_pln_day": deposit_day_pln,
        "electricity_bill_pln_day": bill_day_pln,
        "net_cashflow_pln_day": deposit_day_pln - bill_day_pln,
        "net_export_surplus_kwh": net_export_kwh_pos,
        "net_import_surplus_kwh": net_import_kwh_pos,
    }

    return {
        "date": local_date.isoformat(),
        "telemetry_rows": len(rows),
        "pricing_source": pricing.get("source"),
        "method": {
            "energy_balance": "Między pełnymi godzinami: pierwszy pomiar w H i pierwszy w H+1 na licznikach E_imp/E_exp.",
            "deposit": "Gdy net_kWh > 0: nadwyżka × RCE godzinowe → wpływ do depozytu.",
            "bill": "Gdy net_kWh < 0: nadwyżka importu × (dystrybucja + energia stała wg strefy G12 w tej godzinie).",
        },
        "warnings": boundary_warnings,
        "totals": totals,
        "hours": hours,
    }


_DASHBOARD_UI_PATH = Path(__file__).resolve().parent / "dashboard_ui.html"
_DASHBOARD_JS_PATH = Path(__file__).resolve().parent / "dashboard.js"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    try:
        return _DASHBOARD_UI_PATH.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"dashboard UI missing: {e}") from e


@app.get("/dashboard.js")
def dashboard_js() -> HTMLResponse:
    try:
        return HTMLResponse(
            _DASHBOARD_JS_PATH.read_text(encoding="utf-8"),
            media_type="application/javascript",
        )
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"dashboard JS missing: {e}") from e


@app.get("/api/history")
def api_history(limit: int = Query(default=200, ge=1, le=5000)) -> JSONResponse:
    rows = read_history(limit=limit)
    annotate_history_closing_balance(rows)
    payload = {
        "log_path": str(LOG_PATH),
        "rows": [{"fields": r.fields, "raw": r.raw} for r in rows],
    }
    return JSONResponse(payload)


@app.get("/api/baseline")
def api_baseline() -> JSONResponse:
    return JSONResponse(baseline_spec())


@app.get("/api/status")
def api_status() -> JSONResponse:
    rows = read_history(limit=1)
    row = rows[0] if rows else None
    payload = {
        "log_path": str(LOG_PATH),
        "fields": (row.fields if row else {}),
        "raw": (row.raw if row else None),
        "watchdog_soc": watchdog_soc_api_payload(),
    }
    return JSONResponse(payload)


@app.get("/api/guardian/watchdog-soc")
def api_watchdog_soc_get() -> JSONResponse:
    return JSONResponse(watchdog_soc_api_payload())


@app.put("/api/guardian/watchdog-soc")
def api_watchdog_soc_put(
    body: WatchdogSocUpdateBody,
    _: None = Depends(_require_guardian_api_key),
) -> JSONResponse:
    apply_watchdog_override_updates(body.model_dump(exclude_unset=True))
    return JSONResponse(watchdog_soc_api_payload())


@app.delete("/api/guardian/watchdog-soc")
def api_watchdog_soc_delete(
    _: None = Depends(_require_guardian_api_key),
) -> JSONResponse:
    clear_watchdog_override()
    return JSONResponse(watchdog_soc_api_payload())


@app.get("/api/pricing/day")
def api_pricing_day(
    day: str | None = Query(default=None, description="YYYY-MM-DD (domyślnie dziś)"),
) -> JSONResponse:
    local_date = date.fromisoformat(day) if day else date.today()
    payload = pricing_day_breakdown(local_date)
    return JSONResponse(payload)


@app.get("/api/pv-forecast")
def api_pv_forecast(hours: int = Query(default=48, ge=1, le=96)) -> JSONResponse:
    try:
        payload = fetch_hourly_pv_forecast(hours=hours)
    except RuntimeError as e:
        logger.warning("pv-forecast 503: %s", e)
        raise HTTPException(status_code=503, detail=str(e)) from e
    except httpx.HTTPError as e:
        logger.warning("pv-forecast 503 (proxy): %s", e)
        raise HTTPException(
            status_code=503,
            detail=f"Solcast proxy error: {e}",
        ) from e
    return JSONResponse(payload)


@app.get("/api/load-forecast")
def api_load_forecast(
    hours: int = Query(default=24, ge=1, le=168),
    lookback_days: int = Query(default=28, ge=7, le=120),
) -> JSONResponse:
    payload = forecast_load_hours(hours=hours, lookback_days=lookback_days)
    return JSONResponse(payload)


@app.get("/api/load-forecast/backtest")
def api_load_forecast_backtest(
    lookback_days: int = Query(default=28, ge=7, le=120),
    max_days: int | None = Query(
        default=None,
        ge=1,
        le=366,
        description="Ostatnie N dni; None = wszystkie dni w cache",
    ),
) -> JSONResponse:
    payload = run_load_forecast_backtest(
        lookback_days=lookback_days,
        max_days=max_days,
        progress=False,
    )
    return JSONResponse(payload)


_KPI_CACHE_TTL_S = 60.0
_kpi_cache: tuple[date, float, dict[str, Any]] | None = None


def _get_kpi_today_cached() -> dict[str, Any]:
    global _kpi_cache
    today = date.today()
    mono = time.monotonic()
    cached = _kpi_cache
    if cached is not None:
        d, at, payload = cached
        if d == today and (mono - at) < _KPI_CACHE_TTL_S:
            return payload
    payload = _kpi_for_day(today)
    _kpi_cache = (today, mono, payload)
    return payload


@app.get("/api/kpi/today")
async def api_kpi_today() -> JSONResponse:
    payload = await _run_heavy(_get_kpi_today_cached)
    return JSONResponse(payload)


# Memoize tomorrow's pricing fetch to avoid hammering PSE/proxy every dashboard
# refresh while RCE for tomorrow is not yet published. Cache both success and
# failure for a short TTL; on success the underlying RCE module also persists
# its own on-disk cache, so subsequent process starts are cheap too.
_TOMORROW_PRICING_TTL_S = 300.0
_tomorrow_pricing_cache: tuple[date, float, dict[str, Any] | None] | None = None

_COMBINED_FORECAST_TTL_S = 90.0
_combined_forecast_cache: tuple[float, dict[str, Any]] | None = None

_ECOSLOTS_CACHE_TTL_S = 20.0
_ecoslots_cache: tuple[float, dict[str, Any]] | None = None


def _first_pv_kwh_per_hour(local_date: date) -> dict[int, float]:
    """
    Najwcześniejsza próbka E_pv_kwh (licznik e_total z inwertera) per godzina lokalna.
    Zwraca {hour: kwh}. Godziny bez próbki lub bez pola E_pv_kwh — pominięte.
    """
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
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not (0 <= hour <= 23):
                    continue
                val = row.get("E_pv_kwh")
                if val is None:
                    continue
                try:
                    v = float(val)
                except (TypeError, ValueError):
                    continue
                prev = best.get(hour)
                if prev is None or minute < prev[0]:
                    best[hour] = (minute, v)
    except OSError:
        return {}
    return {h: v for h, (_m, v) in best.items()}


def _telemetry_hourly_load_pv_actuals(
    local_date: date,
) -> tuple[dict[int, float], dict[int, float]]:
    """
    Load: średnia consumption_w / 1000 → przybliżone kWh w godzinie (jak baseline load).
    PV:   Δ E_pv_kwh między pierwszą próbką godziny H a pierwszą próbką H+1
          (z licznika ``e_total`` w inwerterze). Dla H=23 używa pierwszej próbki
          z pliku telemetrii następnego dnia.
    """
    path = TELEMETRY_DIR / f"telemetry_{local_date.isoformat()}.jsonl"
    buckets_c: dict[int, list[float]] = {h: [] for h in range(24)}
    if not path.exists():
        return {}, {}
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    hour = int(row["local_hour"])
                    cw = float(row["consumption_w"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if 0 <= hour <= 23:
                    buckets_c[hour].append(cw)
    except OSError:
        return {}, {}

    load_kwh: dict[int, float] = {}
    for h in range(24):
        if buckets_c[h]:
            load_kwh[h] = (sum(buckets_c[h]) / len(buckets_c[h])) / 1000.0

    first_pv_today = _first_pv_kwh_per_hour(local_date)
    first_pv_tomorrow = _first_pv_kwh_per_hour(local_date + timedelta(days=1))

    pv_kwh: dict[int, float] = {}
    for h in range(24):
        start = first_pv_today.get(h)
        if start is None:
            continue
        if h < 23:
            end = first_pv_today.get(h + 1)
        else:
            end = first_pv_tomorrow.get(0)
        if end is None:
            continue
        delta = end - start
        if delta < 0:
            continue
        pv_kwh[h] = delta
    return load_kwh, pv_kwh


def _planner_hours_for_date(local_date: date) -> dict[int, Any]:
    """Godzina → HourPlan z rolling planu (``plan_latest.json``)."""
    plan = load_latest_plan() or load_plan(local_date.isoformat())
    if plan is None:
        return {}
    d_iso = local_date.isoformat()
    return {hp.hour: hp for hp in plan.hours if hp.date == d_iso}


def _pricing_for_day_quiet(local_date: date) -> dict[str, Any] | None:
    """Like pricing_day_breakdown but returns None on any error (and caches that)."""
    global _tomorrow_pricing_cache
    now = time.monotonic()
    cached = _tomorrow_pricing_cache
    if cached is not None:
        cached_date, cached_at, cached_val = cached
        if cached_date == local_date and (now - cached_at) < _TOMORROW_PRICING_TTL_S:
            return cached_val
    try:
        val: dict[str, Any] | None = pricing_day_breakdown(local_date)
    except Exception as e:
        logger.info("pricing for %s not available yet: %s", local_date.isoformat(), e)
        val = None
    _tomorrow_pricing_cache = (local_date, now, val)
    return val


def _combined_forecast_payload() -> dict[str, Any]:
    """48 hours starting today 00:00 with RCE/G12, load forecast and PV forecast merged."""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    lookback_days = 28
    cache_min = today - timedelta(days=lookback_days + 2)
    cache = build_daily_hourly_kwh_cache(min_date=cache_min)

    try:
        pricing_today: dict[str, Any] | None = pricing_day_breakdown(today)
    except Exception as e:
        logger.warning("pricing for today not available: %s", e)
        pricing_today = None
    pricing_tomorrow = _pricing_for_day_quiet(tomorrow)

    def price_lookup(pricing: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
        if not pricing:
            return {}
        return {int(h["hour"]): h for h in pricing.get("hours", [])}

    price_today = price_lookup(pricing_today)
    price_tomorrow = price_lookup(pricing_tomorrow)

    try:
        pv_payload = fetch_hourly_pv_forecast_with_history(hours_back=48, hours_forward=48)
    except (RuntimeError, httpx.HTTPError) as e:
        logger.warning("pv forecast (history+forecasts) unavailable: %s", e)
        pv_payload = {"hours": []}
    pv_by_dh = {
        (str(h.get("date")), int(h.get("hour"))): h for h in pv_payload.get("hours", [])
    }

    now = datetime.now(ZoneInfo(TELEMETRY_TZ)).replace(tzinfo=None)
    load_payload = forecast_load_hours(
        start_dt=now, hours=48, lookback_days=lookback_days, cache=cache
    )
    load_by_dh = {
        (str(h.get("date")), int(h.get("hour"))): h
        for h in load_payload.get("hours", [])
    }
    actuals_today = _telemetry_hourly_load_pv_actuals(today)
    actuals_tomorrow = _telemetry_hourly_load_pv_actuals(tomorrow)
    actuals_by_date: dict[str, tuple[dict[int, float], dict[int, float]]] = {
        today.isoformat(): actuals_today,
        tomorrow.isoformat(): actuals_tomorrow,
    }
    planner_today = _planner_hours_for_date(today)
    planner_tomorrow = _planner_hours_for_date(tomorrow)
    telemetry_today = hourly_actuals(today)
    telemetry_tomorrow = hourly_actuals(tomorrow)

    rows: list[dict[str, Any]] = []
    start_dt = datetime.combine(today, datetime.min.time())
    for offset in range(48):
        slot = start_dt + timedelta(hours=offset)
        d = slot.date()
        h = slot.hour
        d_iso = d.isoformat()
        slot_end = slot + timedelta(hours=1)
        hour_complete = slot_end <= now

        if d == today:
            p = price_today.get(h)
        elif d == tomorrow:
            p = price_tomorrow.get(h)
        else:
            p = None

        pv_row = pv_by_dh.get((d_iso, h))
        load_row = load_by_dh.get((d_iso, h))
        if load_row is not None:
            load_p25 = float(load_row["load_kwh_p25"])
            load_p50 = float(load_row["load_kwh_p50"])
            load_p75 = float(load_row["load_kwh_p75"])
        else:
            base = predict_load_one_hour(d, h, lookback_days, cache)
            load_p25 = float(base["load_kwh_p25"])
            load_p50 = float(base["load_kwh_p50"])
            load_p75 = float(base["load_kwh_p75"])

        act_load_map, act_pv_map = actuals_by_date.get(d_iso, ({}, {}))
        load_actual = act_load_map.get(h) if hour_complete else None
        pv_actual = act_pv_map.get(h) if hour_complete else None

        pv_mean = float(pv_row["pv_kw"]) if pv_row and pv_row.get("pv_kw") is not None else None
        load_delta_p50 = (
            float(load_actual) - load_p50
            if hour_complete and load_actual is not None
            else None
        )
        pv_delta_mean = (
            float(pv_actual) - pv_mean
            if hour_complete
            and pv_actual is not None
            and pv_mean is not None
            else None
        )

        if d == today:
            plan_h = planner_today.get(h)
            tel_h = telemetry_today.get(h)
        elif d == tomorrow:
            plan_h = planner_tomorrow.get(h)
            tel_h = telemetry_tomorrow.get(h)
        else:
            plan_h = None
            tel_h = None

        net_kwh: float | None
        soc_pct: float | None
        if tel_h is not None and (hour_complete or tel_h.get("samples", 0) > 0):
            net_kwh = float(tel_h["net_kwh"])
            soc_pct = float(tel_h["last_soc_pct"])
        elif plan_h is not None:
            net_kwh = float(plan_h.target_net_kwh)
            soc_pct = float(plan_h.soc_end_pct)
        else:
            net_kwh = None
            soc_pct = None

        rows.append(
            {
                "date": d_iso,
                "hour": h,
                "hour_complete": hour_complete,
                "buy_pln_kwh": p.get("import_pln_per_kwh") if p else None,
                "sell_pln_kwh": p.get("rce_pln_kwh") if p else None,
                "load_kwh_p25": load_p25,
                "load_kwh_p50": load_p50,
                "load_kwh_p75": load_p75,
                "load_kwh_actual": load_actual,
                "load_kwh_delta_p50": load_delta_p50,
                "pv_kwh": pv_mean,
                "pv_kwh_p10": float(pv_row["pv_kw_p10"]) if pv_row and pv_row.get("pv_kw_p10") is not None else None,
                "pv_kwh_p90": float(pv_row["pv_kw_p90"]) if pv_row and pv_row.get("pv_kw_p90") is not None else None,
                "pv_kwh_actual": pv_actual,
                "pv_kwh_delta_mean": pv_delta_mean,
                "net_kwh": net_kwh,
                "soc_pct": soc_pct,
            }
        )

    plan_latest = load_latest_plan()
    plan_exec, plan_exec_src = effective_planner_execution_enabled()
    plan_note = ""
    if plan_latest is not None:
        plan_note = (
            f" Plan: {plan_latest.plan_id[:8]}… {plan_latest.horizon_start}→{plan_latest.horizon_end}."
            f" Egzekucja w Guardianie: {'tak' if plan_exec else 'nie'} ({plan_exec_src})."
        )
    else:
        plan_note = " Brak plan_latest.json — uruchom: uv run python -m planner plan."

    return {
        "now": now.isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "tomorrow": tomorrow.isoformat(),
        "pricing_today_source": (pricing_today or {}).get("source"),
        "pricing_tomorrow_source": (pricing_tomorrow or {}).get("source"),
        "pricing_tomorrow_available": pricing_tomorrow is not None,
        "load_nowcast": load_payload.get("nowcast", {}),
        "comparison_note": (
            "Zakończone godziny: act load ≈ średnia consumption_w/1000 z telemetrii (kWh/h); "
            "act PV = przyrost licznika e_total inwertera w tej godzinie (dokładne kWh, "
            "first(H+1) − first(H)). Prognoza PV: średnia moc kW × 1h = kWh (sloty 30 min "
            "z Solcast zagregowane do godziny). Prognoza obciążenia: baseline z historii; "
            "dla slotów od „teraz” w przód — wartości z API z korektą nowcast (jeśli włączona). "
            "Δ load = act − p50; Δ PV = act − mean tylko gdy jest prognoza mean w tym wierszu. "
            "net kWh / SOC %: po zakończeniu godziny (lub bieżąca z telemetrii) — fakty; "
            "w przód — target_net_kwh i soc_end_pct z rolling planu (plan_latest.json). "
            "Horyzont: bieżąca godzina → ostatnia z cenami RCE "
            "(jutro wchodzi automatycznie po publikacji RCE). "
            "Przełącznik w ustawieniach steruje tylko egzekucją w Guardianie, nie liczeniem planu."
        ) + plan_note,
        "rows": rows,
    }


def _get_combined_forecast_cached() -> dict[str, Any]:
    global _combined_forecast_cache
    mono = time.monotonic()
    cached = _combined_forecast_cache
    if cached is not None and (mono - cached[0]) < _COMBINED_FORECAST_TTL_S:
        return cached[1]
    with _forecast_build_lock:
        cached = _combined_forecast_cache
        if cached is not None and (mono - cached[0]) < _COMBINED_FORECAST_TTL_S:
            return cached[1]
        payload = _combined_forecast_payload()
        _combined_forecast_cache = (time.monotonic(), payload)
        return payload


@app.get("/api/forecast/combined")
async def api_forecast_combined() -> JSONResponse:
    payload = await _run_heavy(_get_combined_forecast_cached)
    return JSONResponse(payload)


@app.get("/api/ecoslots")
async def api_ecoslots_get(refresh: bool = Query(default=False)) -> JSONResponse:
    global _ecoslots_cache
    mono = time.monotonic()
    if not refresh:
        cached = _ecoslots_cache
        if cached is not None and (mono - cached[0]) < _ECOSLOTS_CACHE_TTL_S:
            return JSONResponse(cached[1])
        snap = load_ecoslots_payload_from_snapshot()
        if snap is not None:
            _ecoslots_cache = (mono, snap)
            return JSONResponse(snap)
    try:
        payload = await fetch_ecoslots_payload(live=True)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e)) from e
    except Exception as e:
        logger.warning("ecoslots read failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e)) from e
    _ecoslots_cache = (mono, payload)
    return JSONResponse(payload)


@app.put("/api/ecoslots/{slot_id}")
async def api_ecoslots_put(
    slot_id: str,
    body: EcoslotWriteBody,
    _: None = Depends(_require_guardian_api_key),
) -> JSONResponse:
    global _ecoslots_cache
    if slot_id not in editable_slot_ids():
        raise HTTPException(
            status_code=400,
            detail=f"Slot {slot_id} jest zarezerwowany dla Guardiana ({balancing_slot_id()})",
        )
    try:
        result = await write_ecoslot(
            slot_id,
            start_h=body.start_h,
            start_m=body.start_m,
            end_h=body.end_h,
            end_m=body.end_m,
            power=body.power,
            days=body.days,
            soc=body.soc,
            months=body.months,
            enabled=body.enabled,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e)) from e
    except Exception as e:
        logger.warning("ecoslot write %s failed: %s", slot_id, e)
        raise HTTPException(status_code=502, detail=str(e)) from e
    _ecoslots_cache = None
    return JSONResponse(result)


@app.get("/api/guardian/control")
def api_guardian_control_get(
    _: None = Depends(_require_guardian_api_key),
) -> JSONResponse:
    enabled, source = effective_control_enabled()
    return JSONResponse({"control_enabled": enabled, "source": source})


@app.put("/api/guardian/control")
def api_guardian_control_put(
    body: GuardianControlBody,
    _: None = Depends(_require_guardian_api_key),
) -> JSONResponse:
    write_control_override(body.control_enabled)
    enabled, source = effective_control_enabled()
    return JSONResponse({"control_enabled": enabled, "source": source})


@app.get("/api/guardian/planner")
def api_planner_control_get(
    _: None = Depends(_require_guardian_api_key),
) -> JSONResponse:
    enabled, source = effective_planner_execution_enabled()
    plan = load_latest_plan()
    payload: dict[str, Any] = {
        "planner_execution_enabled": enabled,
        "source": source,
    }
    if plan is not None:
        payload["plan_id"] = plan.plan_id
        payload["horizon_start"] = plan.horizon_start
        payload["horizon_end"] = plan.horizon_end
        payload["generated_at"] = plan.generated_at
    return JSONResponse(payload)


@app.put("/api/guardian/planner")
def api_planner_control_put(
    body: PlannerControlBody,
    _: None = Depends(_require_guardian_api_key),
) -> JSONResponse:
    write_planner_execution_override(body.planner_execution_enabled)
    enabled, source = effective_planner_execution_enabled()
    return JSONResponse({"planner_execution_enabled": enabled, "source": source})
