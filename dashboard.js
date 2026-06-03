function dv(v, d) { return v !== null && v !== undefined ? v : d; }
const fmt = (v) => (v === null || v === undefined) ? "—" : v;
function card(key, val) {
  return `<div class="card"><div class="k">${key}</div><div class="v">${fmt(val)}</div></div>`;
}
function getKey() { return (localStorage.getItem("guardianApiKey") || "").trim(); }

const pageLoaded = {};
let currentPage = null;
let pollTimer = null;

async function fetchJson(url, timeoutMs = 25000) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: ac.signal });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || r.statusText || `HTTP ${r.status}`);
    return j;
  } catch (e) {
    if (e.name === "AbortError") throw new Error(`timeout ${timeoutMs / 1000}s`);
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

function setUpdated(ok) {
  document.getElementById("updatedAt").textContent = ok ? new Date().toLocaleTimeString() : "błąd";
}

async function loadOverview(force) {
  if (!force && pageLoaded.overview) return;
  try {
    const status = await fetchJson("/api/status", 10000);
    document.getElementById("logPath").textContent = status.log_path || "—";
    const f = status.fields || {};
    document.getElementById("cards").innerHTML = [
      ["ts", f.ts], ["remaining_kwh", f.remaining_kwh], ["balancing_kw", f.balancing_kw],
      ["grid_kw", f.grid_kw], ["pv_kw", f.pv_kw], ["house_w", f.house_w],
      ["soc_pct", f.soc_pct], ["p_bat_w", f.p_bat_w], ["time_to_end_s", f.time_to_end_s],
      ["ecoslot_read_pct", f.ecoslot_read_pct], ["intervene", f.intervene], ["reason", f.reason],
      ["cmd_enabled", f.cmd_enabled], ["cmd_pct", f.cmd_pct], ["cmd_duration_s", f.cmd_duration_s],
    ].map(([k, v]) => card(k, v)).join("");
    pageLoaded.overview = true;
    setUpdated(true);
  } catch (e) {
    setUpdated(false);
    console.error(e);
  }
}

async function loadHistory(force) {
  if (!force && pageLoaded.history) return;
  const st = document.getElementById("historyStatus");
  st.textContent = "ładowanie…";
  try {
    const hist = await fetchJson("/api/history?limit=200", 15000);
    document.getElementById("hist").innerHTML = (hist.rows || []).map((r) => {
      const f = r.fields || {};
      const cmd = (f.cmd_enabled === null) ? "—" : `${f.cmd_enabled ? "On" : "Off"} ${fmt(f.cmd_pct)}% ${fmt(f.cmd_duration_s)}s`;
      const closing = f.closing_prev_hour_kwh;
      const balCell = (closing !== null && closing !== undefined)
        ? `<td title="bilans końcowy poprzedniej godziny (∑)">${fmt(closing)} <span class="muted">∑</span></td>`
        : `<td>${fmt(f.remaining_kwh)}</td>`;
      return `<tr><td>${fmt(f.ts)}</td>${balCell}<td>${fmt(f.grid_kw)}</td><td>${fmt(f.pv_kw)}</td><td>${fmt(f.house_w)}</td><td>${fmt(f.soc_pct)}</td><td>${fmt(f.p_bat_w)}</td><td class="muted">${fmt(f.reason)}</td><td class="muted">${cmd}</td></tr>`;
    }).join("");
    pageLoaded.history = true;
    st.textContent = "OK";
    setUpdated(true);
  } catch (e) {
    st.textContent = String(e);
    setUpdated(false);
  }
}

function renderForecastBlock(forecast) {
  const fcell = (v, d) => (v == null) ? `<td class="nodata">—</td>` : `<td>${Number(v).toFixed(d)}</td>`;
  const fcellDelta = (v, d, eps) => {
    if (v == null) return `<td class="nodata">—</td>`;
    const n = Number(v);
    let cls = "delta-ok";
    if (n > eps) cls = "delta-pos";
    else if (n < -eps) cls = "delta-neg";
    return `<td class="${cls}">${(n >= 0 ? "+" : "") + n.toFixed(d)}</td>`;
  };
  const nowDate = (forecast.now || "").slice(0, 10);
  const nowHour = Number((forecast.now || "T00").slice(11, 13));
  let prevDate = null;
  document.getElementById("forecastRows").innerHTML = (forecast.rows || []).map((r) => {
    const cls = [];
    if (prevDate && r.date !== prevDate) cls.push("day-break");
    if (r.date === nowDate && r.hour === nowHour) cls.push("now");
    if (r.date < nowDate || (r.date === nowDate && r.hour < nowHour)) cls.push("past");
    prevDate = r.date;
    const trClass = cls.length ? ` class="${cls.join(" ")}"` : "";
    return `<tr${trClass}><td>${r.date.slice(5)}</td><td>${String(r.hour).padStart(2, "0")}:00</td>
      ${fcell(r.buy_pln_kwh, 4)}${fcell(r.sell_pln_kwh, 4)}
      ${fcell(r.load_kwh_p25, 3)}${fcell(r.load_kwh_p50, 3)}${fcell(r.load_kwh_p75, 3)}${fcell(r.load_kwh_actual, 3)}${fcellDelta(r.load_kwh_delta_p50, 3, 0.03)}
      ${fcell(r.pv_kwh_p10, 3)}${fcell(r.pv_kwh, 3)}${fcell(r.pv_kwh_p90, 3)}${fcell(r.pv_kwh_actual, 3)}${fcellDelta(r.pv_kwh_delta_mean, 3, 0.05)}
      ${fcell(r.net_kwh, 3)}${fcell(r.soc_pct, 1)}</tr>`;
  }).join("");
  const meta = [`today RCE: ${fmt(forecast.pricing_today_source)}`,
    forecast.pricing_tomorrow_available ? `tomorrow RCE: ${fmt(forecast.pricing_tomorrow_source)}` : "tomorrow RCE: not yet published"];
  document.getElementById("forecastMeta").textContent = meta.join(" · ");
  const cn = document.getElementById("forecastCompareNote");
  if (cn) cn.textContent = forecast.comparison_note || "";
  const nc = forecast.load_nowcast || {};
  document.getElementById("loadNowcast").textContent = nc.applied
    ? `load nowcast: bias ${Number(nc.bias_w || 0).toFixed(0)} W; decay ${dv(nc.decay_hours, "—")} h`
    : (nc.reason ? "nowcast off — " + nc.reason : "");
}

async function loadForecast(force) {
  if (!force && pageLoaded.forecast) return;
  const st = document.getElementById("forecastStatus");
  st.textContent = "ładowanie…";
  try {
    const forecast = await fetchJson("/api/forecast/combined", 60000);
    renderForecastBlock(forecast);
    pageLoaded.forecast = true;
    st.textContent = "OK";
    setUpdated(true);
  } catch (e) {
    st.textContent = String(e);
    document.getElementById("forecastMeta").textContent = "błąd: " + e;
    setUpdated(false);
  }
}

function renderKpiBlock(kpi) {
  const totals = kpi.totals || {};
  document.getElementById("kpiCards").innerHTML = [
    ["deposit_add_pln_day", Number(totals.deposit_add_pln_day || 0).toFixed(2)],
    ["electricity_bill_pln_day", Number(totals.electricity_bill_pln_day || 0).toFixed(2)],
    ["net_cashflow_pln_day", Number(totals.net_cashflow_pln_day || 0).toFixed(2)],
    ["net_export_surplus_kwh", Number(totals.net_export_surplus_kwh || 0).toFixed(3)],
    ["net_import_surplus_kwh", Number(totals.net_import_surplus_kwh || 0).toFixed(3)],
    ["telemetry_rows", fmt(kpi.telemetry_rows)],
    ["pricing_source", fmt(kpi.pricing_source)],
  ].map(([k, v]) => card(k, v)).join("");
  const warns = kpi.warnings || [];
  document.getElementById("kpiWarnings").textContent = warns.length
    ? `KPI warnings (${warns.length}): ${warns.slice(0, 5).join(" · ")}` : "";
}

async function loadKpi(force) {
  if (!force && pageLoaded.kpi) return;
  const st = document.getElementById("kpiStatus");
  st.textContent = "ładowanie…";
  try {
    renderKpiBlock(await fetchJson("/api/kpi/today", 30000));
    pageLoaded.kpi = true;
    st.textContent = "OK";
    setUpdated(true);
  } catch (e) {
    st.textContent = String(e);
    document.getElementById("kpiWarnings").textContent = "KPI: " + e;
    setUpdated(false);
  }
}

function hourArraysEqual(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  const sa = [...a].map(Number).sort((x, y) => x - y);
  const sb = [...b].map(Number).sort((x, y) => x - y);
  return sa.every((v, i) => v === sb[i]);
}
function flEq(a, b) { return Math.abs(Number(a) - Number(b)) < 1e-6; }

function renderWatchdogSoc(wds) {
  if (!wds || !wds.effective) return;
  const eff = wds.effective, src = wds.sources || {};
  document.getElementById("wdPathLine").textContent =
    `File ${wds.override_path || "—"} — ${wds.override_exists ? "override present" : "env only"}`;
  document.getElementById("watchdogSummaryCards").innerHTML = [
    ["soc_night_reserve_pct", `${fmt(eff.soc_night_reserve_pct)} (${fmt(src.soc_night_reserve_pct)})`],
    ["soc_night_reserve_charge_pct", `${fmt(eff.soc_night_reserve_charge_pct)} (${fmt(src.soc_night_reserve_charge_pct)})`],
    ["soc_night_reserve_hours", `${(eff.soc_night_reserve_hours || []).join(",")}`],
    ["soc_low_defense_threshold_pct", `${fmt(eff.soc_low_defense_threshold_pct)}`],
    ["soc_full_defense_threshold_pct", `${fmt(eff.soc_full_defense_threshold_pct)}`],
  ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v" style="font-size:16px;">${v}</div></div>`).join("");
  document.getElementById("wd_soc_night_reserve_pct").value = eff.soc_night_reserve_pct;
  document.getElementById("wd_soc_night_reserve_charge_pct").value = eff.soc_night_reserve_charge_pct;
  document.getElementById("wd_soc_night_reserve_hours").value = (eff.soc_night_reserve_hours || []).join(",");
  document.getElementById("wd_soc_low_defense_threshold_pct").value = eff.soc_low_defense_threshold_pct;
  document.getElementById("wd_soc_full_defense_threshold_pct").value = eff.soc_full_defense_threshold_pct;
}

async function loadSettings(force) {
  try {
    const wds = await fetchJson("/api/guardian/watchdog-soc", 10000);
    window._lastWds = wds;
    renderWatchdogSoc(wds);
    if (getKey()) refreshControl().catch(console.error);
  } catch (e) {
    console.error(e);
  }
  if (!force && pageLoaded.ecoslots) return;
  await refreshEcoslots(false);
  pageLoaded.ecoslots = true;
  pageLoaded.settings = true;
}

async function refreshControl() {
  const key = getKey();
  const el = document.getElementById("controlStatus");
  if (!key) { el.textContent = "ustaw klucz API"; return; }
  const r = await fetch("/api/guardian/control", { headers: { "X-Guardian-Api-Key": key } });
  const j = await r.json().catch(() => ({}));
  el.textContent = r.ok ? `enabled=${j.control_enabled} source=${j.source}` : (j.detail || "error");
}

async function putControl(enabled) {
  const key = getKey();
  if (!key) { alert("Ustaw klucz API"); return; }
  const r = await fetch("/api/guardian/control", {
    method: "PUT",
    headers: { "Content-Type": "application/json", "X-Guardian-Api-Key": key },
    body: JSON.stringify({ control_enabled: enabled }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { alert(j.detail || "error"); return; }
  document.getElementById("controlStatus").textContent = `enabled=${j.control_enabled} source=${j.source}`;
}

async function saveWatchdog() {
  const key = getKey(), st = document.getElementById("wdSaveStatus");
  if (!key) { st.textContent = "Ustaw klucz API"; return; }
  const eb = (window._lastWds || {}).env_base;
  if (!eb) { st.textContent = "Brak konfiguracji"; return; }
  const body = {};
  const snr = parseFloat(document.getElementById("wd_soc_night_reserve_pct").value);
  const src = parseInt(document.getElementById("wd_soc_night_reserve_charge_pct").value, 10);
  const slow = parseFloat(document.getElementById("wd_soc_low_defense_threshold_pct").value);
  const sfull = parseFloat(document.getElementById("wd_soc_full_defense_threshold_pct").value);
  if ([snr, slow, sfull].some(Number.isNaN) || Number.isNaN(src)) { st.textContent = "Złe liczby"; return; }
  body.soc_night_reserve_pct = flEq(snr, eb.soc_night_reserve_pct) ? null : snr;
  body.soc_night_reserve_charge_pct = src === eb.soc_night_reserve_charge_pct ? null : src;
  body.soc_low_defense_threshold_pct = flEq(slow, eb.soc_low_defense_threshold_pct) ? null : slow;
  body.soc_full_defense_threshold_pct = flEq(sfull, eb.soc_full_defense_threshold_pct) ? null : sfull;
  const hrs = document.getElementById("wd_soc_night_reserve_hours").value.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !Number.isNaN(n));
  body.soc_night_reserve_hours = hourArraysEqual(hrs, eb.soc_night_reserve_hours || []) ? null : hrs;
  const r = await fetch("/api/guardian/watchdog-soc", {
    method: "PUT", headers: { "Content-Type": "application/json", "X-Guardian-Api-Key": key },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { st.textContent = j.detail || "error"; return; }
  st.textContent = "Zapisano.";
  window._lastWds = j;
  renderWatchdogSoc(j);
}

async function resetWatchdog() {
  const key = getKey(), st = document.getElementById("wdSaveStatus");
  if (!key) { st.textContent = "Ustaw klucz API"; return; }
  const r = await fetch("/api/guardian/watchdog-soc", { method: "DELETE", headers: { "X-Guardian-Api-Key": key } });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { st.textContent = j.detail || "error"; return; }
  st.textContent = "Override wyczyszczony.";
  window._lastWds = j;
  renderWatchdogSoc(j);
}

function ecoTime(h, m) {
  if (h == null) return "—";
  return String(h).padStart(2, "0") + ":" + String(dv(m, 0)).padStart(2, "0");
}

function ecoTimeInput(h, m) {
  if (h == null || Number.isNaN(Number(h))) return "";
  return ecoTime(h, m);
}

function snapSlot(slotId) {
  return (window._lastEcoslots && window._lastEcoslots.slots && window._lastEcoslots.slots[slotId]) || {};
}

function showEcoStatus(msg) {
  const st = document.getElementById("ecoSlotsStatus");
  st.textContent = msg;
  st.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function parseEcoTime(value) {
  if (!value || !String(value).includes(":")) return [NaN, NaN];
  const parts = String(value).split(":");
  const h = parseInt(parts[0], 10);
  const m = parseInt(parts[1], 10);
  return [h, m];
}

function readEcoTimeFields(slotId) {
  const startEl = document.querySelector(`[data-eco="${slotId}"][data-field="start_time"]`);
  const endEl = document.querySelector(`[data-eco="${slotId}"][data-field="end_time"]`);
  let [start_h, start_m] = parseEcoTime(startEl && startEl.value);
  let [end_h, end_m] = parseEcoTime(endEl && endEl.value);
  const snap = snapSlot(slotId);
  if (Number.isNaN(start_h)) {
    start_h = snap.start_h;
    start_m = dv(snap.start_m, 0);
  }
  if (Number.isNaN(end_h)) {
    end_h = snap.end_h;
    end_m = dv(snap.end_m, 0);
  }
  return [start_h, start_m, end_h, end_m];
}

const ECO_DAY_PRESETS = [
  ["Mon-Sun", "Cały tydzień"],
  ["Mon-Fri", "Pn–Pt"],
  ["Sat-Sun", "So–Nd"],
];

function bindEcoSlotForm(slotId) {
  const range = document.querySelector(`[data-eco="${slotId}"][data-field="power"]`);
  const valEl = document.querySelector(`[data-eco="${slotId}"][data-field="power_val"]`);
  if (range && valEl) {
    const sync = () => { valEl.textContent = `${range.value}%`; };
    range.addEventListener("input", sync);
    sync();
  }
  const sel = document.querySelector(`[data-eco="${slotId}"][data-field="days_select"]`);
  const custom = document.querySelector(`[data-eco="${slotId}"][data-field="days"]`);
  if (sel && custom) {
    const toggle = () => {
      const isCustom = sel.value === "__custom__";
      custom.style.display = isCustom ? "block" : "none";
      if (!isCustom) custom.value = sel.value;
    };
    sel.addEventListener("change", toggle);
    toggle();
  }
}

function renderEcoslots(data) {
  if (!data) return;
  document.getElementById("ecoBalancingSlot").textContent = data.balancing_slot_id || "—";
  document.getElementById("ecoSlotsPanels").innerHTML = (data.editable_slot_ids || []).map((sid) => {
    const s = (data.slots || {})[sid] || {};
    const pwr = dv(s.power_pct, 0);
    const days = s.days || "Mon-Sun";
    const isPreset = ECO_DAY_PRESETS.some(([v]) => v === days);
    const dayOpts = ECO_DAY_PRESETS.map(([v, label]) =>
      `<option value="${v}" ${days === v ? "selected" : ""}>${label}</option>`
    ).join("") + `<option value="__custom__" ${!isPreset ? "selected" : ""}>Własne…</option>`;
    const active = s.active_now ? '<span class="tag">ACTIVE</span>' : "";
    return `<article class="card eco-slot-card" data-eco-card="${sid}">
      <div class="eco-slot-title">${sid.replace("eco_mode_", "Slot ")} ${active}</div>
      <div class="muted eco-slot-read">Odczyt: ${ecoTime(s.start_h, s.start_m)} – ${ecoTime(s.end_h, s.end_m)} · ${fmt(s.power_pct)}%</div>
      <div class="eco-form">
        <div class="eco-time-grid">
          <label class="eco-field"><span>Od</span>
            <input data-eco="${sid}" data-field="start_time" type="time" ${ecoTimeInput(s.start_h, s.start_m) ? `value="${ecoTimeInput(s.start_h, s.start_m)}"` : ""} /></label>
          <label class="eco-field"><span>Do</span>
            <input data-eco="${sid}" data-field="end_time" type="time" ${ecoTimeInput(s.end_h, s.end_m) ? `value="${ecoTimeInput(s.end_h, s.end_m)}"` : ""} /></label>
        </div>
        <label class="eco-field"><span>Moc baterii (ujemne = ładowanie, dodatnie = rozładowanie)</span>
          <div class="eco-power-row">
            <input data-eco="${sid}" data-field="power" type="range" min="-100" max="100" step="1" value="${pwr}" />
            <span class="eco-power-val" data-eco="${sid}" data-field="power_val">${pwr}%</span>
          </div>
        </label>
        <label class="eco-field"><span>Dni tygodnia</span>
          <select data-eco="${sid}" data-field="days_select">${dayOpts}</select>
          <input data-eco="${sid}" data-field="days" class="eco-days-custom" type="text"
            value="${days}" placeholder="np. Mon,Tue,Wed" style="display:${isPreset ? "none" : "block"}" />
        </label>
        <label class="eco-field"><span>SoC docelowy [%] (10–100)</span>
          <input data-eco="${sid}" data-field="soc" type="number" min="10" max="100" step="1" value="${Math.min(100, Math.max(10, dv(s.soc_pct, 100)))}" inputmode="numeric" /></label>
        <label class="eco-enabled">
          <input data-eco="${sid}" data-field="enabled" type="checkbox" ${s.enabled ? "checked" : ""} />
          <span>Harmonogram włączony</span>
        </label>
        <button type="button" class="btn-eco-save" data-eco-save="${sid}">Zapisz ${sid.replace("eco_mode_", "slot ")}</button>
      </div>
    </article>`;
  }).join("") || '<div class="muted">Brak slotów.</div>';
  (data.editable_slot_ids || []).forEach((sid) => bindEcoSlotForm(sid));
}

let _ecoSaveInFlight = false;

function handleEcoSaveEvent(e) {
  const btn = e.target.closest("[data-eco-save]");
  if (!btn) return;
  e.preventDefault();
  const slotId = btn.getAttribute("data-eco-save");
  const run = () => saveEcoslot(slotId, btn).catch((err) => {
    console.error(err);
    showEcoStatus(String(err));
  });
  const ae = document.activeElement;
  const editing = ae instanceof HTMLInputElement || ae instanceof HTMLSelectElement;
  if (editing && ae !== btn) {
    ae.blur();
    setTimeout(run, 100);
    return;
  }
  run();
}

async function refreshEcoslots(live = false) {
  const st = document.getElementById("ecoSlotsStatus");
  st.textContent = live ? "odczyt z inwertera…" : "ładowanie snapshot…";
  try {
    const url = live ? "/api/ecoslots?refresh=1" : "/api/ecoslots";
    const j = await fetchJson(url, live ? 20000 : 5000);
    window._lastEcoslots = j;
    renderEcoslots(j);
    const src = j.source === "runner" ? "runner" : (live ? "inwerter" : j.source || "snapshot");
    const at = j.read_at ? j.read_at.slice(11, 19) : "";
    st.textContent = `OK (${src}${at ? " " + at : ""})`;
  } catch (e) { st.textContent = String(e); }
}

async function saveEcoslot(slotId, btn) {
  if (_ecoSaveInFlight) return;
  _ecoSaveInFlight = true;
  const key = getKey();
  if (!key) {
    _ecoSaveInFlight = false;
    showEcoStatus("Ustaw klucz API powyżej");
    document.getElementById("apiKey").focus();
    alert("Ustaw klucz API w sekcji Guardian control (ten sam co na laptopie).");
    return;
  }
  const label = btn && btn.textContent;
  if (btn) {
    btn.classList.add("is-saving");
    btn.disabled = true;
    btn.textContent = "Zapisuję…";
  }
  showEcoStatus(`Zapisuję ${slotId}…`);
  try {
    const pick = (field) => {
      const el = document.querySelector(`[data-eco="${slotId}"][data-field="${field}"]`);
      if (!el) return null;
      if (el.type === "checkbox") return el.checked;
      if (field === "days") {
        const sel = document.querySelector(`[data-eco="${slotId}"][data-field="days_select"]`);
        if (sel && sel.value !== "__custom__") return sel.value;
        return el.value.trim() || "Mon-Sun";
      }
      if (field === "power") return parseInt(el.value, 10);
      return parseInt(el.value, 10);
    };
    const [start_h, start_m, end_h, end_m] = readEcoTimeFields(slotId);
    let soc = pick("soc");
    if (Number.isNaN(soc)) soc = dv(snapSlot(slotId).soc_pct, 100);
    const body = {
      start_h, start_m, end_h, end_m,
      power: pick("power"),
      days: pick("days"),
      soc,
      enabled: pick("enabled"),
    };
    if ([body.start_h, body.start_m, body.end_h, body.end_m, body.power, body.soc].some(Number.isNaN)) {
      showEcoStatus("Sprawdź godzinę i liczby");
      return;
    }
    if (body.soc < 10 || body.soc > 100) {
      showEcoStatus("SoC docelowy: 10–100");
      return;
    }
    const r = await fetch(`/api/ecoslots/${slotId}`, {
      method: "PUT", headers: { "Content-Type": "application/json", "X-Guardian-Api-Key": key },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      showEcoStatus(typeof j.detail === "string" ? j.detail : (j.detail || "error"));
      return;
    }
    showEcoStatus("Zapisano " + slotId);
    await refreshEcoslots(false);
  } finally {
    _ecoSaveInFlight = false;
    if (btn) {
      btn.classList.remove("is-saving");
      btn.disabled = false;
      btn.textContent = label || `Zapisz ${slotId.replace("eco_mode_", "slot ")}`;
    }
  }
}

const PAGE_LOADERS = {
  overview: loadOverview,
  history: loadHistory,
  forecast: loadForecast,
  kpi: loadKpi,
  settings: loadSettings,
};

function navigate(page, force) {
  if (!PAGE_LOADERS[page]) page = "overview";
  if (currentPage === page && !force) return;
  currentPage = page;
  document.querySelectorAll(".page").forEach((el) => el.classList.remove("active"));
  document.getElementById("page-" + page).classList.add("active");
  document.querySelectorAll("#mainNav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === page);
  });
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  PAGE_LOADERS[page](!!force);
  if (page === "overview") {
    pollTimer = setInterval(() => loadOverview(true), 15000);
  }
}

function parsePageFromHash() {
  const h = (location.hash || "#overview").replace(/^#/, "");
  return PAGE_LOADERS[h] ? h : "overview";
}

document.getElementById("mainNav").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-page]");
  if (!a) return;
  e.preventDefault();
  location.hash = a.dataset.page;
});

window.addEventListener("hashchange", () => navigate(parsePageFromHash(), false));

document.getElementById("btnRefreshHistory").addEventListener("click", () => { pageLoaded.history = false; loadHistory(true); });
document.getElementById("btnRefreshForecast").addEventListener("click", () => { pageLoaded.forecast = false; loadForecast(true); });
document.getElementById("btnRefreshKpi").addEventListener("click", () => { pageLoaded.kpi = false; loadKpi(true); });
document.getElementById("saveKey").addEventListener("click", () => {
  localStorage.setItem("guardianApiKey", document.getElementById("apiKey").value.trim());
  refreshControl().catch(console.error);
});
document.getElementById("refreshControl").addEventListener("click", () => refreshControl().catch(console.error));
document.getElementById("enableControl").addEventListener("click", () => putControl(true));
document.getElementById("disableControl").addEventListener("click", () => putControl(false));
document.getElementById("saveWatchdog").addEventListener("click", () => saveWatchdog().catch(console.error));
document.getElementById("resetWatchdog").addEventListener("click", () => resetWatchdog().catch(console.error));
document.getElementById("refreshEcoslots").addEventListener("click", () => refreshEcoslots(true).catch(console.error));

const ecoPanels = document.getElementById("ecoSlotsPanels");
ecoPanels.addEventListener("click", handleEcoSaveEvent);

document.getElementById("apiKey").value = getKey();
if (!location.hash) location.hash = "overview";
navigate(parsePageFromHash(), true);