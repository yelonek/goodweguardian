from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

import guardian_config as guardian_cfg

from energy_pricing import pricing_day_breakdown
from guardian_config import LOG_DIR, TELEMETRY_DIR
from guardian_control import effective_control_enabled, write_control_override
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

app = FastAPI(title="GoodWeGuardian Dashboard", version="0.1.0")

logger = logging.getLogger(__name__)

LOG_PATH = Path(os.environ.get("GUARDIAN_LOG_PATH") or (LOG_DIR / "guardian.log"))


class GuardianControlBody(BaseModel):
    control_enabled: bool


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


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>GoodWeGuardian Dashboard</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; margin: 18px; }
    .grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; }
    .grid4 { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .card { border: 1px solid rgba(127,127,127,0.35); border-radius: 10px; padding: 10px 12px; }
    .k { opacity: 0.75; font-size: 12px; }
    .v { font-size: 18px; font-weight: 700; }
    .row { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
    .tag { border: 1px solid rgba(127,127,127,0.35); border-radius: 999px; padding: 2px 8px; font-size: 12px; opacity: 0.9; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; }
    th, td { border-bottom: 1px solid rgba(127,127,127,0.25); padding: 6px 8px; text-align: left; vertical-align: top; }
    th { position: sticky; top: 0; background: rgba(0,0,0,0.05); backdrop-filter: blur(6px); }
    .muted { opacity: 0.7; }
    table.forecast td, table.forecast th { text-align: right; font-variant-numeric: tabular-nums; }
    table.forecast td:first-child, table.forecast th:first-child,
    table.forecast td:nth-child(2), table.forecast th:nth-child(2) { text-align: left; }
    table.forecast .grp-price { background: rgba(255, 200, 80, 0.10); }
    table.forecast .grp-load { background: rgba(120, 180, 255, 0.10); }
    table.forecast .grp-pv { background: rgba(120, 220, 140, 0.10); }
    table.forecast tr.day-break td { border-top: 2px solid rgba(127,127,127,0.55); }
    table.forecast tr.now td { background: rgba(255, 215, 0, 0.18); font-weight: 700; }
    table.forecast tr.past td { opacity: 0.55; }
    .nodata { opacity: 0.35; }
    table.forecast td.delta-pos { color: #0d8050; }
    table.forecast td.delta-neg { color: #b32d2d; }
    table.forecast td.delta-ok { opacity: 0.85; }
    @media (max-width: 1200px) { .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } .grid4 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 760px) { .grid, .grid4 { grid-template-columns: repeat(1, minmax(0, 1fr)); } }
    details.advanced-panel { margin: 14px 0 18px; }
    details.advanced-panel > summary { cursor: pointer; font-weight: 700; font-size: 1.1em; user-select: none; }
  </style>
</head>
<body>
  <div class="row">
    <div class="tag">log: <span id="logPath">(loading)</span></div>
    <div class="tag">updated: <span id="updatedAt">—</span></div>
  </div>

  <details class="advanced-panel">
    <summary>Guardian control</summary>
    <div class="card" style="max-width: 520px; margin-top: 10px;">
      <div class="k">API key (stored in browser localStorage)</div>
      <input id="apiKey" type="password" placeholder="GUARDIAN_API_KEY" style="width: 100%; margin: 8px 0; padding: 6px;" />
      <div class="row">
        <button type="button" id="saveKey">Save key</button>
        <button type="button" id="refreshControl">Refresh status</button>
        <button type="button" id="enableControl">Enable writes</button>
        <button type="button" id="disableControl">Disable writes</button>
      </div>
      <div class="muted" style="margin-top:8px;">Status: <span id="controlStatus">—</span></div>
      <div class="muted" style="font-size:12px;">
        Same switch as <code>state/guardian_control_override.json</code> (<code>control_enabled</code>): these buttons only write that file.
        While the file exists, it overrides <code>GUARDIAN_CONTROL_ENABLED</code> in <code>.env</code> (no process restart). Delete the file to follow <code>.env</code> again.
        Runner and dashboard must share the same <code>state/</code> directory.
      </div>
    </div>
  </details>

  <details class="advanced-panel">
    <summary>Watchdog / SOC config</summary>
    <div class="muted" style="margin: 10px 0 8px;">
      Effective values after merging <code>.env</code> (at process start) with optional
      <code>state/guardian_watchdog_override.json</code>. The hourly runner reloads that file every cycle — no restart.
      Dashboard and runner must share the same <code>state/</code> directory (same as control override).
    </div>
    <div class="grid" id="watchdogSummaryCards"></div>
    <div class="card" style="max-width: 520px; margin-top: 10px;">
      <div class="k" id="wdPathLine">…</div>
      <div style="display:grid; gap:8px; max-width: 440px; margin-top:8px;">
        <label class="row">soc_night_reserve_pct <input id="wd_soc_night_reserve_pct" type="number" step="0.1" min="0" max="100" style="max-width: 8rem;" /></label>
        <label class="row">soc_night_reserve_charge_pct <input id="wd_soc_night_reserve_charge_pct" type="number" step="1" min="-1" max="100" style="max-width: 8rem;" /></label>
        <label class="row">soc_night_reserve_hours (CSV) <input id="wd_soc_night_reserve_hours" type="text" placeholder="22,23,0,1,2,3,4,5" style="width: 100%;" /></label>
        <label class="row">soc_low_defense_threshold_pct <input id="wd_soc_low_defense_threshold_pct" type="number" step="0.1" min="0" max="100" style="max-width: 8rem;" /></label>
        <label class="row">soc_full_defense_threshold_pct <input id="wd_soc_full_defense_threshold_pct" type="number" step="0.1" min="0" max="100" style="max-width: 8rem;" /></label>
      </div>
      <div class="row" style="margin-top:12px;">
        <button type="button" id="saveWatchdog">Save overrides</button>
        <button type="button" id="resetWatchdog">Clear overrides</button>
      </div>
      <div class="muted" id="wdSaveStatus" style="margin-top:8px;"></div>
    </div>
  </details>

  <h2>Current state</h2>
  <div class="grid" id="cards"></div>

  <h2>KPI today</h2>
  <div class="grid4" id="kpiCards"></div>
  <div class="muted" id="kpiWarnings" style="margin-top:8px;"></div>

  <h2>Forecast (today + tomorrow)</h2>
  <div class="muted" id="forecastMeta" style="margin-top:4px;"></div>
  <div class="muted" id="forecastCompareNote" style="font-size:11px; max-width:960px; margin-top:6px; line-height:1.35;"></div>
  <div class="muted" id="loadNowcast" style="margin-top:4px;"></div>
  <table class="forecast">
    <thead>
      <tr>
        <th rowspan="2">date</th>
        <th rowspan="2">hour</th>
        <th colspan="2" class="grp-price">price [PLN/kWh]</th>
        <th colspan="5" class="grp-load">load [kWh]</th>
        <th colspan="5" class="grp-pv">PV [kWh]</th>
      </tr>
      <tr>
        <th class="grp-price" title="Import netto w tej godzinie: TARIFF_DISTRIBUTION + TARIFF_ENERGY (dzień lub noc G12: 22–6, 13–15 = noc)">buy</th>
        <th class="grp-price" title="Eksport netto w tej godzinie: RCE PLN/kWh">sell</th>
        <th class="grp-load">p25</th>
        <th class="grp-load">p50</th>
        <th class="grp-load">p75</th>
        <th class="grp-load" title="Średnia consumption_w/1000 z telemetrii — tylko po zakończeniu godziny">act</th>
        <th class="grp-load" title="act − p50 (w tej kolumnie p50)">Δ</th>
        <th class="grp-pv">p10</th>
        <th class="grp-pv">mean</th>
        <th class="grp-pv">p90</th>
        <th class="grp-pv" title="Przyrost licznika e_total inwertera w tej godzinie (kWh) — tylko po zakończeniu godziny">act</th>
        <th class="grp-pv" title="act − mean, gdy jest prognoza mean">Δ</th>
      </tr>
    </thead>
    <tbody id="forecastRows"></tbody>
  </table>

  <h2>History (newest first)</h2>
  <table>
    <thead>
      <tr>
        <th>ts</th>
        <th>remaining_kwh</th>
        <th>grid_kw</th>
        <th>pv_kw</th>
        <th>house_w</th>
        <th>soc</th>
        <th>p_bat_w</th>
        <th>reason</th>
        <th>cmd</th>
      </tr>
    </thead>
    <tbody id="hist"></tbody>
  </table>

<script>
const fmt = (v) => (v === null || v === undefined) ? "—" : v;

function card(key, val) {
  return `<div class="card"><div class="k">${key}</div><div class="v">${fmt(val)}</div></div>`;
}

function getKey() { return (localStorage.getItem("guardianApiKey") || "").trim(); }

function hourArraysEqual(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  const sa = [...a].map(Number).sort((x, y) => x - y);
  const sb = [...b].map(Number).sort((x, y) => x - y);
  return sa.every((v, i) => v === sb[i]);
}

function flEq(a, b) {
  return Math.abs(Number(a) - Number(b)) < 1e-6;
}

async function saveWatchdog() {
  const key = getKey();
  const st = document.getElementById("wdSaveStatus");
  if (!key) { st.textContent = "Set API key first"; return; }
  const wds = window._lastWds;
  const eb = wds && wds.env_base;
  if (!eb) { st.textContent = "No config loaded yet"; return; }
  const body = {};
  const snr = parseFloat(document.getElementById("wd_soc_night_reserve_pct").value);
  const src = parseInt(document.getElementById("wd_soc_night_reserve_charge_pct").value, 10);
  const slow = parseFloat(document.getElementById("wd_soc_low_defense_threshold_pct").value);
  const sfull = parseFloat(document.getElementById("wd_soc_full_defense_threshold_pct").value);
  if ([snr, slow, sfull].some((x) => Number.isNaN(x)) || Number.isNaN(src)) {
    st.textContent = "Invalid number in form";
    return;
  }
  body.soc_night_reserve_pct = flEq(snr, eb.soc_night_reserve_pct) ? null : snr;
  body.soc_night_reserve_charge_pct = (src === eb.soc_night_reserve_charge_pct) ? null : src;
  body.soc_low_defense_threshold_pct = flEq(slow, eb.soc_low_defense_threshold_pct) ? null : slow;
  body.soc_full_defense_threshold_pct = flEq(sfull, eb.soc_full_defense_threshold_pct) ? null : sfull;
  const rawH = document.getElementById("wd_soc_night_reserve_hours").value;
  const hrs = rawH.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !Number.isNaN(n));
  const ebh = eb.soc_night_reserve_hours || [];
  body.soc_night_reserve_hours = hourArraysEqual(hrs, ebh) ? null : hrs;
  try {
    const r = await fetch("/api/guardian/watchdog-soc", {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-Guardian-Api-Key": key },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { st.textContent = j.detail || r.statusText || "error"; return; }
    st.textContent = "Saved.";
    window._lastWds = j;
    renderWatchdogSoc(j);
  } catch (e) {
    st.textContent = String(e);
  }
}

async function resetWatchdog() {
  const key = getKey();
  const st = document.getElementById("wdSaveStatus");
  if (!key) { st.textContent = "Set API key first"; return; }
  try {
    const r = await fetch("/api/guardian/watchdog-soc", {
      method: "DELETE",
      headers: { "X-Guardian-Api-Key": key },
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { st.textContent = j.detail || r.statusText || "error"; return; }
    st.textContent = "Overrides cleared.";
    window._lastWds = j;
    renderWatchdogSoc(j);
  } catch (e) {
    st.textContent = String(e);
  }
}

function renderWatchdogSoc(wds) {
  if (!wds || !wds.effective) return;
  const eff = wds.effective;
  const src = wds.sources || {};
  const line = document.getElementById("wdPathLine");
  if (line) {
    line.textContent = `File ${wds.override_path || "—"} — ${wds.override_exists ? "override present" : "no override (env only)"}`;
  }
  const wcards = [
    ["soc_night_reserve_pct", `${fmt(eff.soc_night_reserve_pct)} <span class="muted">(${fmt(src.soc_night_reserve_pct)})</span>`],
    ["soc_night_reserve_charge_pct", `${fmt(eff.soc_night_reserve_charge_pct)} <span class="muted">(${fmt(src.soc_night_reserve_charge_pct)})</span>`],
    ["soc_night_reserve_hours", `${(eff.soc_night_reserve_hours || []).join(",")} <span class="muted">(${fmt(src.soc_night_reserve_hours)})</span>`],
    ["soc_low_defense_threshold_pct", `${fmt(eff.soc_low_defense_threshold_pct)} <span class="muted">(${fmt(src.soc_low_defense_threshold_pct)})</span>`],
    ["soc_full_defense_threshold_pct", `${fmt(eff.soc_full_defense_threshold_pct)} <span class="muted">(${fmt(src.soc_full_defense_threshold_pct)})</span>`],
  ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v" style="font-size:16px;">${v}</div></div>`).join("");
  document.getElementById("watchdogSummaryCards").innerHTML = wcards;
  document.getElementById("wd_soc_night_reserve_pct").value = eff.soc_night_reserve_pct;
  document.getElementById("wd_soc_night_reserve_charge_pct").value = eff.soc_night_reserve_charge_pct;
  document.getElementById("wd_soc_night_reserve_hours").value = (eff.soc_night_reserve_hours || []).join(",");
  document.getElementById("wd_soc_low_defense_threshold_pct").value = eff.soc_low_defense_threshold_pct;
  document.getElementById("wd_soc_full_defense_threshold_pct").value = eff.soc_full_defense_threshold_pct;
}

async function refreshControl() {
  const key = getKey();
  const el = document.getElementById("controlStatus");
  if (!key) { el.textContent = "set API key"; return; }
  const r = await fetch("/api/guardian/control", { headers: { "X-Guardian-Api-Key": key } });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { el.textContent = (j.detail || r.statusText || "error"); return; }
  el.textContent = `enabled=${j.control_enabled} source=${j.source}`;
}

async function putControl(enabled) {
  const key = getKey();
  if (!key) { alert("Set API key first"); return; }
  const r = await fetch("/api/guardian/control", {
    method: "PUT",
    headers: { "Content-Type": "application/json", "X-Guardian-Api-Key": key },
    body: JSON.stringify({ control_enabled: enabled }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { alert(j.detail || r.statusText || "error"); return; }
  document.getElementById("controlStatus").textContent = `enabled=${j.control_enabled} source=${j.source}`;
}

document.getElementById("saveKey").addEventListener("click", () => {
  const v = document.getElementById("apiKey").value.trim();
  localStorage.setItem("guardianApiKey", v);
  refreshControl().catch(console.error);
});
document.getElementById("refreshControl").addEventListener("click", () => refreshControl().catch(console.error));
document.getElementById("enableControl").addEventListener("click", () => putControl(true));
document.getElementById("disableControl").addEventListener("click", () => putControl(false));
document.getElementById("saveWatchdog").addEventListener("click", () => saveWatchdog().catch(console.error));
document.getElementById("resetWatchdog").addEventListener("click", () => resetWatchdog().catch(console.error));

async function refresh() {
  const [statusRes, histRes, forecastRes, kpiRes] = await Promise.all([
    fetch("/api/status"),
    fetch("/api/history?limit=200"),
    fetch("/api/forecast/combined"),
    fetch("/api/kpi/today"),
  ]);
  const status = await statusRes.json();
  const hist = await histRes.json();
  const forecast = await forecastRes.json();
  const kpi = await kpiRes.json();

  document.getElementById("logPath").textContent = status.log_path || "—";
  document.getElementById("updatedAt").textContent = new Date().toLocaleTimeString();

  window._lastWds = status.watchdog_soc || {};
  renderWatchdogSoc(window._lastWds);

  const f = status.fields || {};
  const cards = [
    ["ts", f.ts],
    ["remaining_kwh", f.remaining_kwh],
    ["balancing_kw", f.balancing_kw],
    ["grid_kw", f.grid_kw],
    ["pv_kw", f.pv_kw],
    ["house_w", f.house_w],
    ["soc_pct", f.soc_pct],
    ["p_bat_w", f.p_bat_w],
    ["time_to_end_s", f.time_to_end_s],
    ["ecoslot_read_pct", f.ecoslot_read_pct],
    ["intervene", f.intervene],
    ["reason", f.reason],
    ["cmd_enabled", f.cmd_enabled],
    ["cmd_pct", f.cmd_pct],
    ["cmd_duration_s", f.cmd_duration_s],
  ].map(([k,v]) => card(k, v)).join("");
  document.getElementById("cards").innerHTML = cards;

  const totals = kpi.totals || {};
  const kpiCards = [
    ["deposit_add_pln_day", Number(totals.deposit_add_pln_day || 0).toFixed(2)],
    ["electricity_bill_pln_day", Number(totals.electricity_bill_pln_day || 0).toFixed(2)],
    ["net_cashflow_pln_day", Number(totals.net_cashflow_pln_day || 0).toFixed(2)],
    ["net_export_surplus_kwh", Number(totals.net_export_surplus_kwh || 0).toFixed(3)],
    ["net_import_surplus_kwh", Number(totals.net_import_surplus_kwh || 0).toFixed(3)],
    ["telemetry_rows", fmt(kpi.telemetry_rows)],
    ["pricing_source", fmt(kpi.pricing_source)],
  ].map(([k,v]) => card(k, v)).join("");
   document.getElementById("kpiCards").innerHTML = kpiCards;
  const warns = kpi.warnings || [];
  const wEl = document.getElementById("kpiWarnings");
  wEl.textContent = warns.length
    ? `KPI warnings (${warns.length}): ${warns.slice(0, 5).join(" · ")}${warns.length > 5 ? " …" : ""}`
    : "";

  const fcell = (v, digits) => (v === null || v === undefined)
    ? `<td class="nodata">—</td>`
    : `<td>${Number(v).toFixed(digits)}</td>`;

  const fcellDelta = (v, digits, eps) => {
    if (v === null || v === undefined) return `<td class="nodata">—</td>`;
    const n = Number(v);
    let cls = "delta-ok";
    if (n > eps) cls = "delta-pos";
    else if (n < -eps) cls = "delta-neg";
    const s = (n >= 0 ? "+" : "") + n.toFixed(digits);
    return `<td class="${cls}">${s}</td>`;
  };

  const fcRows = forecast.rows || [];
  const nowDate = (forecast.now || "").slice(0, 10);
  const nowHour = Number((forecast.now || "T00").slice(11, 13));
  let prevDate = null;
  const fcHtml = fcRows.map(r => {
    const cls = [];
    if (prevDate && r.date !== prevDate) cls.push("day-break");
    if (r.date === nowDate && r.hour === nowHour) cls.push("now");
    if (r.date < nowDate || (r.date === nowDate && r.hour < nowHour)) cls.push("past");
    prevDate = r.date;
    const trClass = cls.length ? ` class="${cls.join(" ")}"` : "";
    return `<tr${trClass}>
      <td>${r.date.slice(5)}</td>
      <td>${String(r.hour).padStart(2, "0")}:00</td>
      ${fcell(r.buy_pln_kwh, 4)}
      ${fcell(r.sell_pln_kwh, 4)}
      ${fcell(r.load_kwh_p25, 3)}
      ${fcell(r.load_kwh_p50, 3)}
      ${fcell(r.load_kwh_p75, 3)}
      ${fcell(r.load_kwh_actual, 3)}
      ${fcellDelta(r.load_kwh_delta_p50, 3, 0.03)}
      ${fcell(r.pv_kwh_p10, 3)}
      ${fcell(r.pv_kwh, 3)}
      ${fcell(r.pv_kwh_p90, 3)}
      ${fcell(r.pv_kwh_actual, 3)}
      ${fcellDelta(r.pv_kwh_delta_mean, 3, 0.05)}
    </tr>`;
  }).join("");
  document.getElementById("forecastRows").innerHTML = fcHtml;

  const meta = [];
  meta.push(`today RCE: ${fmt(forecast.pricing_today_source)}`);
  meta.push(forecast.pricing_tomorrow_available
    ? `tomorrow RCE: ${fmt(forecast.pricing_tomorrow_source)}`
    : `tomorrow RCE: not yet published`);
  document.getElementById("forecastMeta").textContent = meta.join(" · ");
  const cn = document.getElementById("forecastCompareNote");
  if (cn) cn.textContent = forecast.comparison_note || "";

  const nc = forecast.load_nowcast || {};
  document.getElementById("loadNowcast").textContent = nc.applied
    ? `load nowcast: bias ${Number(nc.bias_w || 0).toFixed(0)} W (ostatnie ${nc.window_min ?? "—"} min vs baseline p50 × 1000); decay ${nc.decay_hours ?? "—"} h, max Δ ${Number(nc.max_delta_kwh ?? 0).toFixed(2)} kWh/h`
    : `load nowcast: ${nc.reason ? "off — " + nc.reason : "—"}`;

  const rows = (hist.rows || []).map(r => {
    const f = r.fields || {};
    const cmd = (f.cmd_enabled === null) ? "—" : `${f.cmd_enabled ? "On" : "Off"} ${fmt(f.cmd_pct)}% ${fmt(f.cmd_duration_s)}s`;
    return `<tr>
      <td>${fmt(f.ts)}</td>
      <td>${fmt(f.remaining_kwh)}</td>
      <td>${fmt(f.grid_kw)}</td>
      <td>${fmt(f.pv_kw)}</td>
      <td>${fmt(f.house_w)}</td>
      <td>${fmt(f.soc_pct)}</td>
      <td>${fmt(f.p_bat_w)}</td>
      <td class="muted">${fmt(f.reason)}</td>
      <td class="muted">${cmd}</td>
    </tr>`;
  }).join("");
  document.getElementById("hist").innerHTML = rows;
}

document.getElementById("apiKey").value = getKey();
refresh().catch(console.error);
refreshControl().catch(console.error);
setInterval(() => refresh().catch(console.error), 15000);
</script>
</body>
</html>"""


@app.get("/api/history")
def api_history(limit: int = Query(default=200, ge=1, le=5000)) -> JSONResponse:
    rows = read_history(limit=limit)
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


@app.get("/api/kpi/today")
def api_kpi_today() -> JSONResponse:
    payload = _kpi_for_day(date.today())
    return JSONResponse(payload)


# Memoize tomorrow's pricing fetch to avoid hammering PSE/proxy every dashboard
# refresh while RCE for tomorrow is not yet published. Cache both success and
# failure for a short TTL; on success the underlying RCE module also persists
# its own on-disk cache, so subsequent process starts are cheap too.
_TOMORROW_PRICING_TTL_S = 300.0
_tomorrow_pricing_cache: tuple[date, float, dict[str, Any] | None] | None = None


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

    now = datetime.now()
    load_payload = forecast_load_hours(start_dt=now, hours=48, lookback_days=lookback_days)
    load_by_dh = {
        (str(h.get("date")), int(h.get("hour"))): h
        for h in load_payload.get("hours", [])
    }

    cache = build_daily_hourly_kwh_cache()
    actuals_today = _telemetry_hourly_load_pv_actuals(today)
    actuals_tomorrow = _telemetry_hourly_load_pv_actuals(tomorrow)
    actuals_by_date: dict[str, tuple[dict[int, float], dict[int, float]]] = {
        today.isoformat(): actuals_today,
        tomorrow.isoformat(): actuals_tomorrow,
    }

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
        base = predict_load_one_hour(d, h, lookback_days, cache)
        if load_row is not None:
            load_p25 = float(load_row["load_kwh_p25"])
            load_p50 = float(load_row["load_kwh_p50"])
            load_p75 = float(load_row["load_kwh_p75"])
        else:
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
            }
        )

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
            "Δ load = act − p50; Δ PV = act − mean tylko gdy jest prognoza mean w tym wierszu."
        ),
        "rows": rows,
    }


@app.get("/api/forecast/combined")
def api_forecast_combined() -> JSONResponse:
    return JSONResponse(_combined_forecast_payload())


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
