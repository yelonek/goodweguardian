from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import guardian_config as guardian_cfg

from energy_pricing import pricing_day_breakdown
from guardian_config import LOG_DIR, TELEMETRY_DIR
from guardian_control import effective_control_enabled, write_control_override
from baseline_info import baseline_spec
from load_forecast import forecast_load_hours, run_load_forecast_backtest
from pv_forecast import fetch_hourly_pv_forecast

app = FastAPI(title="GoodWeGuardian Dashboard", version="0.1.0")

logger = logging.getLogger(__name__)

LOG_PATH = Path(os.environ.get("GUARDIAN_LOG_PATH") or (LOG_DIR / "guardian.log"))


class GuardianControlBody(BaseModel):
    control_enabled: bool


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
    mm_reason = re.search(r"\|\s+interwen=(?:True|False)\s+\|\s+([^|]+?)(?:\s+\|\s+cmd=|$)", line)
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


def _hourly_counter_net_kwh(*, day: date) -> tuple[dict[int, dict[str, Any]], list[str]]:
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
                warnings.append(f"Brak pierwszego pomiaru po godzinie {h:02d} (koniec interwału)")
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
        eff = float(p.get("effective_import_pln_kwh", 0.0))
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
            "bill": "Gdy net_kWh < 0: nadwyżka importu × cena efektywna z taryfy (G12+RCE) → rachunek.",
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
    @media (max-width: 1200px) { .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } .grid4 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 760px) { .grid, .grid4 { grid-template-columns: repeat(1, minmax(0, 1fr)); } }
  </style>
</head>
<body>
  <div class="row">
    <div class="tag">log: <span id="logPath">(loading)</span></div>
    <div class="tag">updated: <span id="updatedAt">—</span></div>
  </div>

  <h2>Guardian control</h2>
  <div class="card" style="max-width: 520px;">
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

  <h2>Current state</h2>
  <div class="grid" id="cards"></div>

  <h2>KPI today</h2>
  <div class="grid4" id="kpiCards"></div>
  <div class="muted" id="kpiWarnings" style="margin-top:8px;"></div>

  <h2>RCE pricing (today)</h2>
  <table>
    <thead>
      <tr>
        <th>hour</th>
        <th>zone</th>
        <th>RCE [PLN/kWh]</th>
        <th>effective import [PLN/kWh]</th>
      </tr>
    </thead>
    <tbody id="pricingRows"></tbody>
  </table>

  <h2>PV forecast (next 24h)</h2>
  <table>
    <thead>
      <tr>
        <th>date</th>
        <th>hour</th>
        <th>pv_kw</th>
        <th>pv_kw_p10</th>
        <th>pv_kw_p90</th>
      </tr>
    </thead>
    <tbody id="pvRows"></tbody>
  </table>

  <h2>Load forecast (next 24h)</h2>
  <table>
    <thead>
      <tr>
        <th>date</th>
        <th>hour</th>
        <th>p25 [kWh]</th>
        <th>p50 [kWh]</th>
        <th>p75 [kWh]</th>
        <th>samples</th>
        <th>source</th>
      </tr>
    </thead>
    <tbody id="loadRows"></tbody>
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

async function refresh() {
  const [statusRes, histRes, pricingRes, pvRes, loadRes, kpiRes] = await Promise.all([
    fetch("/api/status"),
    fetch("/api/history?limit=200"),
    fetch("/api/pricing/day"),
    fetch("/api/pv-forecast?hours=24"),
    fetch("/api/load-forecast?hours=24"),
    fetch("/api/kpi/today"),
  ]);
  const status = await statusRes.json();
  const hist = await histRes.json();
  const pricing = await pricingRes.json();
  const pv = await pvRes.json();
  const load = await loadRes.json();
  const kpi = await kpiRes.json();

  document.getElementById("logPath").textContent = status.log_path || "—";
  document.getElementById("updatedAt").textContent = new Date().toLocaleTimeString();

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

  const pricingRows = (pricing.hours || []).map(h => `<tr>
      <td>${String(h.hour).padStart(2, "0")}:00</td>
      <td>${fmt(h.zone)}</td>
      <td>${Number(h.rce_pln_kwh || 0).toFixed(4)}</td>
      <td>${Number(h.effective_import_pln_kwh || 0).toFixed(4)}</td>
    </tr>`).join("");
  document.getElementById("pricingRows").innerHTML = pricingRows;

  const pvRows = (pv.hours || []).slice(0, 24).map(h => `<tr>
      <td>${fmt(h.date)}</td>
      <td>${String(h.hour).padStart(2, "0")}:00</td>
      <td>${Number(h.pv_kw || 0).toFixed(3)}</td>
      <td>${Number(h.pv_kw_p10 || 0).toFixed(3)}</td>
      <td>${Number(h.pv_kw_p90 || 0).toFixed(3)}</td>
    </tr>`).join("");
  document.getElementById("pvRows").innerHTML = pvRows;

  const loadRows = (load.hours || []).slice(0, 24).map(h => `<tr>
      <td>${fmt(h.date)}</td>
      <td>${String(h.hour).padStart(2, "0")}:00</td>
      <td>${Number(h.load_kwh_p25 || 0).toFixed(3)}</td>
      <td>${Number(h.load_kwh_p50 || 0).toFixed(3)}</td>
      <td>${Number(h.load_kwh_p75 || 0).toFixed(3)}</td>
      <td>${fmt(h.samples)}</td>
      <td>${fmt(h.source)}</td>
    </tr>`).join("");
  document.getElementById("loadRows").innerHTML = loadRows;

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
    }
    return JSONResponse(payload)


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


@app.get("/api/guardian/control")
def api_guardian_control_get(_: None = Depends(_require_guardian_api_key)) -> JSONResponse:
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

