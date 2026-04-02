from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import guardian_config as guardian_cfg

from guardian_config import LOG_DIR
from guardian_control import effective_control_enabled, write_control_override

app = FastAPI(title="GoodWeGuardian Dashboard", version="0.1.0")


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
    .card { border: 1px solid rgba(127,127,127,0.35); border-radius: 10px; padding: 10px 12px; }
    .k { opacity: 0.75; font-size: 12px; }
    .v { font-size: 18px; font-weight: 700; }
    .row { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
    .tag { border: 1px solid rgba(127,127,127,0.35); border-radius: 999px; padding: 2px 8px; font-size: 12px; opacity: 0.9; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; }
    th, td { border-bottom: 1px solid rgba(127,127,127,0.25); padding: 6px 8px; text-align: left; vertical-align: top; }
    th { position: sticky; top: 0; background: rgba(0,0,0,0.05); backdrop-filter: blur(6px); }
    .muted { opacity: 0.7; }
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
  const [statusRes, histRes] = await Promise.all([
    fetch("/api/status"),
    fetch("/api/history?limit=200"),
  ]);
  const status = await statusRes.json();
  const hist = await histRes.json();

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
setInterval(() => refresh().catch(console.error), 2500);
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

