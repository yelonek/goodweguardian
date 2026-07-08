function dv(v, d) { return v !== null && v !== undefined ? v : d; }
const fmt = (v) => (v === null || v === undefined) ? "—" : v;
function card(key, val) {
  return `<div class="card"><div class="k">${key}</div><div class="v">${fmt(val)}</div></div>`;
}
function getKey() { return (localStorage.getItem("guardianApiKey") || "").trim(); }

function formatConfigSource(source) {
  if (source === "override") return "z panelu (plik nadpisania)";
  if (source === "env") return "z .env (domyślna konfiguracja)";
  return fmt(source);
}

function renderToggleStatus(el, enabled, source, labels) {
  const on = Boolean(enabled);
  const stateLabel = on ? (labels.on || "Włączone") : (labels.off || "Wyłączone");
  el.innerHTML =
    `<span class="status-pill ${on ? "status-on" : "status-off"}">${stateLabel}</span>` +
    `<span class="status-source muted">Źródło: ${formatConfigSource(source)}</span>`;
}

const WD_FIELD_LABELS = {
  soc_night_reserve_enabled: "Rezerwa nocna (Guardian)",
  soc_night_reserve_pct: "Min. SOC w nocy",
  soc_night_reserve_charge_pct: "Ładowanie w rezerwie",
  soc_night_reserve_hours: "Godziny rezerwy",
  soc_low_defense_threshold_pct: "Próg niskiej obrony",
  soc_full_defense_threshold_pct: "Próg pełnej obrony",
};

function formatWatchdogSource(source) {
  if (source === "override") return "panel";
  if (source === "env") return ".env";
  return fmt(source);
}

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

/** Bieżąca data/godzina w strefie przeglądarki (≈ Europe/Warsaw u Ciebie). */
function localNowParts() {
  const d = new Date();
  const date = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return { date, hour: d.getHours() };
}

async function loadOverview(force) {
  if (!force && pageLoaded.overview) return;
  try {
    const [status, pyramid] = await Promise.all([
      fetchJson("/api/status", 10000),
      fetchJson("/api/pv-pyramid", 60000).catch((e) => ({ _error: String(e) })),
    ]);
    document.getElementById("logPath").textContent = status.log_path || "—";
    const f = status.fields || {};
    document.getElementById("cards").innerHTML = [
      ["ts", f.ts], ["remaining_kwh", f.remaining_kwh], ["balancing_kw", f.balancing_kw],
      ["grid_kw", f.grid_kw], ["pv_kw", f.pv_kw], ["house_w", f.house_w],
      ["soc_pct", f.soc_pct], ["p_bat_w", f.p_bat_w], ["time_to_end_s", f.time_to_end_s],
      ["ecoslot_read_pct", f.ecoslot_read_pct], ["intervene", f.intervene], ["reason", f.reason],
      ["cmd_enabled", f.cmd_enabled], ["cmd_pct", f.cmd_pct], ["cmd_duration_s", f.cmd_duration_s],
    ].map(([k, v]) => card(k, v)).join("");
    renderPvPyramid(pyramid);
    pageLoaded.overview = true;
    setUpdated(true);
  } catch (e) {
    setUpdated(false);
    console.error(e);
  }
}

function renderPvPyramidTable(segment, tbodyId) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  const total = Number(segment?.pv_total_kwh || 0);
  const above = Number(segment?.above_60_kwh || 0);
  const barMax = total > 0 ? total : 1;
  const tierRows = (segment?.tiers || []).map((t) => {
    const gr = t.threshold_gr;
    const cum = Number(t.cumulative_kwh || 0);
    const layer = Number(t.layer_kwh || 0);
    const pct = Math.min(100, Math.round((cum / barMax) * 100));
    const cls = gr <= 30 ? "pyramid-cheap" : gr <= 50 ? "pyramid-mid" : "";
    return `<tr class="${cls}"><td>&lt; ${gr} gr</td><td>${cum.toFixed(2)}</td><td>${layer.toFixed(2)}</td>
      <td class="pv-bar-wrap"><span class="pv-bar" style="width:${pct}%;"></span></td></tr>`;
  }).join("");
  const abovePct = Math.min(100, Math.round((above / barMax) * 100));
  tbody.innerHTML = tierRows +
    `<tr><td>≥ 60 gr</td><td>${above.toFixed(2)}</td><td>${above.toFixed(2)}</td>
      <td class="pv-bar-wrap"><span class="pv-bar" style="width:${abovePct}%; opacity:0.55;"></span></td></tr>`;
}

function renderPvPyramid(p) {
  const block = document.getElementById("pvPyramidBlock");
  if (!p || p._error) {
    block.style.display = "none";
    return;
  }
  block.style.display = "block";
  const seg = p.segments || {};
  const today = seg.today || {};
  const tomorrow = seg.tomorrow || {};
  const past = today.past || {};
  const remaining = today.remaining || {};
  const todayTotal = today.total || {};
  const tomorrowTotal = tomorrow.total || {};
  const cheapGr = seg.cheap_threshold_gr || 60;

  const remainingCheap = Number(remaining.cheap_kwh || 0);
  document.getElementById("pvPyramidHero").innerHTML =
    `<div class="card"><div class="card-key">Tanio zostało dziś (&lt;${cheapGr} gr)</div>` +
    `<div class="card-val">${remainingCheap.toFixed(1)} kWh</div></div>`;

  document.getElementById("pvPyramidTodaySummary").innerHTML = [
    ["Zostało tanio", `${remainingCheap.toFixed(1)} kWh`],
    ["Zostało PV", `${Number(remaining.pv_total_kwh || 0).toFixed(1)} kWh`],
    ["Było tanio", `${Number(past.cheap_kwh || 0).toFixed(1)} kWh`],
    ["Było PV", `${Number(past.pv_total_kwh || 0).toFixed(1)} kWh`],
    ["Dziś razem tanio", `${Number(todayTotal.cheap_kwh || 0).toFixed(1)} kWh`],
  ].map(([k, v]) => card(k, v)).join("");

  document.getElementById("pvPyramidTomorrowSummary").innerHTML = [
    [`Tanio (&lt;${cheapGr} gr)`, `${Number(tomorrowTotal.cheap_kwh || 0).toFixed(1)} kWh`],
    ["PV razem", `${Number(tomorrowTotal.pv_total_kwh || 0).toFixed(1)} kWh`],
  ].map(([k, v]) => card(k, v)).join("");

  const meta = [
    p.pricing_tomorrow_available ? `jutro RCE: ${p.pricing_tomorrow_source || "ok"}` : "jutro RCE: brak",
    `dziś zostało: ${remaining.hours_with_pv || 0} h PV`,
    `dziś było: ${past.hours_with_pv || 0} h PV`,
    `jutro: ${tomorrowTotal.hours_with_pv || 0} h PV`,
  ];
  document.getElementById("pvPyramidMeta").textContent = meta.join(" · ");
  const warns = p.warnings || [];
  document.getElementById("pvPyramidWarnings").textContent = warns.length
    ? `Uwagi: ${warns.slice(0, 4).join(" · ")}` : "";

  renderPvPyramidTable(remaining, "pvPyramidRowsRemaining");
  renderPvPyramidTable(past, "pvPyramidRowsPast");
  renderPvPyramidTable(todayTotal, "pvPyramidRowsTodayTotal");
  renderPvPyramidTable(tomorrowTotal, "pvPyramidRowsTomorrow");
}

async function loadHistory(force) {
  if (!force && pageLoaded.history) return;
  const st = document.getElementById("historyStatus");
  if (!pageLoaded.history) st.textContent = "ładowanie…";
  try {
    const hist = await fetchJson("/api/history?limit=200", 15000);
    document.getElementById("hist").innerHTML = (hist.rows || []).map((r) => {
      const f = r.fields || {};
      const reasonRaw = String(f.reason || "");
      let reasonShown = reasonRaw;
      const neutralTarget = reasonRaw.match(/mode=neutral target_net=([+-]?\d+(?:\.\d+)?)/);
      if (neutralTarget) {
        const target = Number(neutralTarget[1]);
        if (!Number.isNaN(target)) {
          reasonShown = `neutral (target ${target.toFixed(2)} kWh)`;
        }
      }
      const cmd = (f.cmd_enabled === null) ? "—" : `${f.cmd_enabled ? "On" : "Off"} ${fmt(f.cmd_pct)}% ${fmt(f.cmd_duration_s)}s`;
      const closing = f.closing_prev_hour_kwh;
      const balCell = (closing !== null && closing !== undefined)
        ? `<td title="bilans końcowy poprzedniej godziny (∑)">${fmt(closing)} <span class="muted">∑</span></td>`
        : `<td>${fmt(f.remaining_kwh)}</td>`;
      return `<tr><td>${fmt(f.ts)}</td>${balCell}<td>${fmt(f.grid_kw)}</td><td>${fmt(f.pv_kw)}</td><td>${fmt(f.house_w)}</td><td>${fmt(f.soc_pct)}</td><td>${fmt(f.p_bat_w)}</td><td class="muted">${fmt(reasonShown)}</td><td class="muted">${cmd}</td></tr>`;
    }).join("");
    pageLoaded.history = true;
    st.textContent = "OK";
    setUpdated(true);
  } catch (e) {
    st.textContent = String(e);
    setUpdated(false);
  }
}

function renderEvChargingPanel(ev) {
  if (!ev) return;
  const budget = ev.cheap_budget || {};
  const cheapPv = Number(budget.cheap_pv_kwh || 0);
  const cheapImp = Number(budget.cheap_import_kwh || 0);
  const rec = Number(budget.recommendable_kwh || 0);
  const hero = document.getElementById("evChargingHero");
  if (hero) {
    hero.innerHTML =
      `<div class="card"><div class="card-key">Tanio PV (&lt;60 gr)</div><div class="card-val">${cheapPv.toFixed(1)} kWh</div></div>` +
      `<div class="card"><div class="card-key">Tanio import G12</div><div class="card-val">${cheapImp.toFixed(1)} kWh</div></div>` +
      `<div class="card"><div class="card-key">Razem tanio dziś</div><div class="card-val">${rec.toFixed(1)} kWh</div></div>`;
  }
  const decl = ev.declaration;
  const targetEl = document.getElementById("evTargetKwh");
  const prefEl = document.getElementById("evPreferredHour");
  const powerEl = document.getElementById("evMaxPowerKw");
  if (decl && targetEl && !targetEl.matches(":focus")) {
    targetEl.value = decl.target_kwh != null ? String(decl.target_kwh) : "";
    if (prefEl) prefEl.value = decl.preferred_start_hour != null ? String(decl.preferred_start_hour) : "";
    if (powerEl && decl.max_power_kw != null) powerEl.value = String(decl.max_power_kw);
  }
  const slotsEl = document.getElementById("evChargingSlots");
  const slots = decl ? (ev.slots || []) : (ev.recommended_slots || []);
  if (slotsEl) {
    if (!slots.length) {
      slotsEl.textContent = decl
        ? "Brak przypisanych godzin — zapisz plan ponownie."
        : "Brak deklaracji — podaj cel kWh i zapisz (propozycja slotów pojawi się po zapisie lub w rekomendacji).";
    } else {
      const prefix = decl ? "Sloty planu" : "Propozycja slotów";
      slotsEl.textContent = prefix + ": " + slots.map((s) => `${String(s.hour).padStart(2, "0")}:00 → ${Number(s.kwh).toFixed(1)} kWh`).join(", ");
    }
  }
  const warnEl = document.getElementById("evChargingWarnings");
  if (warnEl) warnEl.textContent = (ev.warnings || []).join(" ");
}

function updateEvChargingAuthHint() {
  const st = document.getElementById("evChargingStatus");
  const saveBtn = document.getElementById("evChargingSave");
  const clearBtn = document.getElementById("evChargingClear");
  const hasKey = !!getKey();
  if (saveBtn) saveBtn.disabled = !hasKey;
  if (clearBtn) clearBtn.disabled = !hasKey;
  if (!st) return;
  if (!hasKey) {
    st.textContent = "Zapis i wyczyszczenie wymagają klucza API — Ustawienia → Zapisz klucz.";
  }
}

async function loadEvChargingPlan() {
  try {
    const plan = await fetchJson("/api/ev-charging/plan", 30000);
    renderEvChargingPanel(plan);
    updateEvChargingAuthHint();
    return plan;
  } catch (e) {
    const st = document.getElementById("evChargingStatus");
    if (st && !getKey()) updateEvChargingAuthHint();
    else if (st) st.textContent = "Plan EV: " + e;
    return null;
  }
}

async function saveEvChargingPlan() {
  const key = getKey();
  if (!key) { alert("Ustaw klucz API w ustawieniach"); return; }
  const target = parseFloat(document.getElementById("evTargetKwh").value);
  if (Number.isNaN(target) || target < 0) { alert("Podaj cel kWh ≥ 0"); return; }
  const prefRaw = document.getElementById("evPreferredHour").value.trim();
  const preferred_start_hour = prefRaw === "" ? null : parseInt(prefRaw, 10);
  if (preferred_start_hour != null && (Number.isNaN(preferred_start_hour) || preferred_start_hour < 0 || preferred_start_hour > 23)) {
    alert("Godzina startu: 0–23 lub puste");
    return;
  }
  const max_power_kw = parseFloat(document.getElementById("evMaxPowerKw").value) || 11;
  const st = document.getElementById("evChargingStatus");
  if (st) st.textContent = "Zapisuję i przeliczam plan…";
  const r = await fetch("/api/ev-charging/plan", {
    method: "PUT",
    headers: { "Content-Type": "application/json", "X-Guardian-Api-Key": key },
    body: JSON.stringify({ target_kwh: target, preferred_start_hour, max_power_kw }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail || "error");
    if (st) st.textContent = msg;
    alert(msg);
    return;
  }
  renderEvChargingPanel(j);
  if (st) {
    if (j.planner && j.planner.replanned) {
      st.textContent = "Zapisano — planer przeliczony.";
    } else if (j.planner && j.planner.reason) {
      st.textContent = "Zapisano deklarację EV, ale planer nie przeliczony: " + j.planner.reason;
    } else {
      st.textContent = "Zapisano.";
    }
  }
  pageLoaded.forecast = false;
  await loadForecast(true);
}

async function clearEvChargingPlan() {
  const key = getKey();
  if (!key) { alert("Ustaw klucz API w ustawieniach"); return; }
  const st = document.getElementById("evChargingStatus");
  const r = await fetch("/api/ev-charging/plan", {
    method: "DELETE",
    headers: { "X-Guardian-Api-Key": key },
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (st) st.textContent = j.detail || "error";
    return;
  }
  document.getElementById("evTargetKwh").value = "";
  document.getElementById("evPreferredHour").value = "";
  if (st) {
    if (j.planner && j.planner.replanned) {
      st.textContent = "Wyczyszczono — planer przeliczony.";
    } else if (j.planner && j.planner.reason) {
      st.textContent = "Wyczyszczono deklarację EV, ale planer nie przeliczony: " + j.planner.reason;
    } else {
      st.textContent = "Wyczyszczono deklarację EV.";
    }
  }
  await loadEvChargingPlan();
  pageLoaded.forecast = false;
  await loadForecast(true);
}

function renderForecastBlock(forecast) {
  const fcell = (v, d) => (v == null) ? `<td class="nodata">—</td>` : `<td>${Number(v).toFixed(d)}</td>`;
  const twcOn = !!forecast.twc_enabled;
  const evPlan = forecast.ev_charging || {};
  const evPlanOn = !!(evPlan.declaration || (evPlan.slots && evPlan.slots.length));
  const forecastTable = document.getElementById("forecastTable");
  if (forecastTable) {
    forecastTable.classList.toggle("twc-on", twcOn);
    forecastTable.classList.toggle("ev-plan-on", evPlanOn);
  }
  const loadColspan = document.getElementById("forecastLoadColspan");
  if (loadColspan) loadColspan.colSpan = (twcOn ? 7 : 5) + (evPlanOn ? 0 : 0);
  const twcNote = document.getElementById("forecastTwcNote");
  if (twcNote) {
    twcNote.style.display = twcOn ? "" : "none";
    twcNote.textContent = twcOn
      ? "EV = licznik Tesla Wall Connector (kWh/h); dom = razem − EV. Tylko zakończone godziny z próbkami TWC."
      : "";
  }
  const evCell = (v) => {
    if (!twcOn) return "";
    if (v == null) return `<td class="nodata twc-col">—</td>`;
    const n = Number(v);
    const cls = n > 0.05 ? " ev-charge" : "";
    return `<td class="grp-load twc-col${cls}" title="Tesla Wall Connector">${n.toFixed(3)}</td>`;
  };
  const domCell = (v) => {
    if (!twcOn) return "";
    if (v == null) return `<td class="nodata twc-col">—</td>`;
    return `<td class="grp-load twc-col">${Number(v).toFixed(3)}</td>`;
  };
  const evPlanCell = (v) => {
    if (!evPlanOn) return "";
    if (v == null || Number(v) <= 0) return `<td class="nodata ev-plan-col">—</td>`;
    return `<td class="grp-load ev-plan-col ev-charge">${Number(v).toFixed(1)}</td>`;
  };
  const policyCell = (r) => {
    if (!r.policy) return `<td class="nodata">—</td>`;
    const title = r.policy + (r.policy_label ? ` (${r.policy_label})` : "");
    const label = r.policy_label || r.policy;
    return `<td class="grp-planner" title="${title}">${label}</td>`;
  };
  const boolCell = (v) => (v == null) ? `<td class="nodata">—</td>` : `<td>${v ? "tak" : "nie"}</td>`;
  const fcellDelta = (v, d, eps) => {
    if (v == null) return `<td class="nodata">—</td>`;
    const n = Number(v);
    let cls = "delta-ok";
    if (n > eps) cls = "delta-pos";
    else if (n < -eps) cls = "delta-neg";
    return `<td class="${cls}">${(n >= 0 ? "+" : "") + n.toFixed(d)}</td>`;
  };
  const { date: nowDate, hour: nowHour } = localNowParts();
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
      ${evCell(r.ev_kwh_actual)}${domCell(r.load_base_kwh_actual)}
      ${evPlanCell(r.ev_planned_kwh)}${evPlanCell(r.load_plan_kwh)}
      ${fcell(r.pv_kwh_p10, 3)}${fcell(r.pv_kwh, 3)}${fcell(r.pv_kwh_p90, 3)}${fcell(r.pv_kwh_actual, 3)}${fcellDelta(r.pv_kwh_delta_mean, 3, 0.05)}
      ${policyCell(r)}${fcell(r.policy_battery_delta_kwh, 3)}${boolCell(r.policy_allow_grid_charge)}
      ${fcell(r.net_kwh, 3)}${fcell(r.soc_pct, 1)}</tr>`;
  }).join("");
  const meta = [`today RCE: ${fmt(forecast.pricing_today_source)}`,
    forecast.pricing_tomorrow_available ? `tomorrow RCE: ${fmt(forecast.pricing_tomorrow_source)}` : "tomorrow RCE: not yet published"];
  document.getElementById("forecastMeta").textContent = meta.join(" · ");
  const cn = document.getElementById("forecastCompareNote");
  if (cn) cn.textContent = forecast.comparison_note || "";
  const nc = forecast.load_nowcast || {};
  document.getElementById("loadNowcast").textContent = nc.applied
    ? `load nowcast: ×${Number(nc.factor || 1).toFixed(2)} (bias ${Number(nc.bias_w || 0).toFixed(0)} W); decay ${dv(nc.decay_hours, "—")} h`
    : (nc.reason ? "nowcast off — " + nc.reason : "");
  if (evPlan && evPlan.declaration) {
    renderEvChargingPanel(evPlan);
  }
  updateEvChargingAuthHint();
}

async function loadForecast(force) {
  if (!force && pageLoaded.forecast) return;
  const st = document.getElementById("forecastStatus");
  if (!pageLoaded.forecast) st.textContent = "ładowanie…";
  try {
    updateEvChargingAuthHint();
    const [forecast] = await Promise.all([
      fetchJson("/api/forecast/combined", 60000),
      loadEvChargingPlan(),
    ]);
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

function kpiSelectedDay() {
  const el = document.getElementById("kpiDay");
  return el && el.value ? el.value : new Date().toISOString().slice(0, 10);
}

function initKpiDayPicker() {
  const el = document.getElementById("kpiDay");
  if (!el) return;
  const today = new Date().toISOString().slice(0, 10);
  el.max = today;
  if (!el.value) el.value = today;
}

function renderKpiBlock(payload) {
  const kpi = payload.kpi || payload;
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

  const audit = payload.audit;
  const auditCards = document.getElementById("kpiAuditCards");
  const auditMeta = document.getElementById("kpiAuditMeta");
  if (!audit) {
    auditCards.innerHTML = "";
    auditMeta.textContent = payload.audit_source === "missing"
      ? "Brak audytu (brak telemetrii dla wybranej doby)."
      : "";
    return;
  }
  auditCards.innerHTML = [
    ["actual_total_cashflow_pln", Number(audit.actual_total_cashflow_pln || 0).toFixed(2)],
    ["perfect_foresight_cashflow_pln", audit.perfect_foresight_cashflow_pln == null ? "—" : Number(audit.perfect_foresight_cashflow_pln).toFixed(2)],
    ["uplift_vs_actual_pln", audit.uplift_vs_actual_pln == null ? "—" : Number(audit.uplift_vs_actual_pln).toFixed(2)],
  ].map(([k, v]) => card(k, v)).join("");
  const src = payload.audit_source ? ` [${payload.audit_source}]` : "";
  auditMeta.textContent = [
    audit.summary_pl || "",
    audit.audited_at ? `audited_at: ${audit.audited_at}${src}` : src.trim(),
  ].filter(Boolean).join(" · ");
}

function renderKpiMergedTable(payload) {
  const fcell = (v, d) => (v == null) ? `<td class="nodata">—</td>` : `<td>${Number(v).toFixed(d)}</td>`;
  const fcellDelta = (v, d, eps) => {
    if (v == null) return `<td class="nodata">—</td>`;
    const n = Number(v);
    let cls = "delta-ok";
    if (n > eps) cls = "delta-pos";
    else if (n < -eps) cls = "delta-neg";
    return `<td class="${cls}">${(n >= 0 ? "+" : "") + n.toFixed(d)}</td>`;
  };
  const day = payload.date || kpiSelectedDay();
  const today = new Date().toISOString().slice(0, 10);
  const nowHour = new Date().getHours();
  document.getElementById("kpiMergedRows").innerHTML = (payload.merged_hours || []).map((r) => {
    const cls = [];
    if (day === today && r.hour === nowHour) cls.push("now");
    else if (day < today || (day === today && r.hour < nowHour)) cls.push("past");
    const trClass = cls.length ? ` class="${cls.join(" ")}"` : "";
    return `<tr${trClass}><td>${String(r.hour).padStart(2, "0")}:00</td>
      ${fcell(r.kpi_net_kwh, 3)}${fcell(r.kpi_deposit_pln, 2)}${fcell(r.kpi_bill_pln, 2)}
      ${fcell(r.audit_net_kwh, 3)}${fcell(r.load_kwh, 3)}${fcell(r.pv_kwh, 3)}
      ${fcell(r.actual_cashflow_pln, 2)}${fcell(r.optimal_cashflow_pln, 2)}${fcellDelta(r.gap_vs_optimal_pln, 2, 0.02)}
    </tr>`;
  }).join("");
}

async function loadKpi(force) {
  if (!force && pageLoaded.kpi) return;
  const st = document.getElementById("kpiStatus");
  if (!pageLoaded.kpi) st.textContent = "ładowanie…";
  try {
    const day = kpiSelectedDay();
    const payload = await fetchJson(`/api/kpi/day?day=${encodeURIComponent(day)}`, 60000);
    renderKpiBlock(payload);
    renderKpiMergedTable(payload);
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

function setNightReserveFieldsEnabled(on) {
  const wrap = document.getElementById("wdNightReserveFields");
  if (wrap) wrap.classList.toggle("muted", !on);
  for (const id of ["wd_soc_night_reserve_pct", "wd_soc_night_reserve_charge_pct", "wd_soc_night_reserve_hours"]) {
    const el = document.getElementById(id);
    if (el) el.disabled = !on;
  }
}

function renderWatchdogSoc(wds) {
  if (!wds || !wds.effective) return;
  const eff = wds.effective, src = wds.sources || {};
  document.getElementById("wdPathLine").textContent = wds.override_exists
    ? "Nadpisania z panelu: aktywne (niektóre progi mogą różnić się od .env)"
    : "Tylko wartości z .env — brak pliku nadpisania";
  document.getElementById("watchdogSummaryCards").innerHTML = [
    ["soc_night_reserve_enabled", eff.soc_night_reserve_enabled ? "włączona" : "wyłączona (planer)", formatWatchdogSource(src.soc_night_reserve_enabled)],
    ["soc_night_reserve_pct", `${fmt(eff.soc_night_reserve_pct)}%`, formatWatchdogSource(src.soc_night_reserve_pct)],
    ["soc_night_reserve_charge_pct", `${fmt(eff.soc_night_reserve_charge_pct)}%`, formatWatchdogSource(src.soc_night_reserve_charge_pct)],
    ["soc_night_reserve_hours", (eff.soc_night_reserve_hours || []).join(", "), formatWatchdogSource(src.soc_night_reserve_hours)],
    ["soc_low_defense_threshold_pct", `${fmt(eff.soc_low_defense_threshold_pct)}%`, formatWatchdogSource(src.soc_low_defense_threshold_pct)],
    ["soc_full_defense_threshold_pct", `${fmt(eff.soc_full_defense_threshold_pct)}%`, formatWatchdogSource(src.soc_full_defense_threshold_pct)],
  ].map(([k, v, s]) => {
    const title = WD_FIELD_LABELS[k] || k;
    return `<div class="card"><div class="k">${title}</div><div class="v" style="font-size:16px;">${v}</div><div class="muted" style="font-size:11px;margin-top:4px;">źródło: ${s}</div></div>`;
  }).join("");
  const nightOn = Boolean(eff.soc_night_reserve_enabled);
  document.getElementById("wd_soc_night_reserve_enabled").checked = nightOn;
  setNightReserveFieldsEnabled(nightOn);
  document.getElementById("wd_soc_night_reserve_pct").value = eff.soc_night_reserve_pct;
  document.getElementById("wd_soc_night_reserve_charge_pct").value = eff.soc_night_reserve_charge_pct;
  document.getElementById("wd_soc_night_reserve_hours").value = (eff.soc_night_reserve_hours || []).join(",");
  document.getElementById("wd_soc_low_defense_threshold_pct").value = eff.soc_low_defense_threshold_pct;
  document.getElementById("wd_soc_full_defense_threshold_pct").value = eff.soc_full_defense_threshold_pct;
}

async function loadSettings(force) {
  const isPoll = force && pageLoaded.settings;
  if (!isPoll) {
    try {
      const wds = await fetchJson("/api/guardian/watchdog-soc", 10000);
      window._lastWds = wds;
      renderWatchdogSoc(wds);
    } catch (e) {
      console.error(e);
    }
  }
  if (getKey()) {
    refreshControl().catch(console.error);
    refreshPlanner().catch(console.error);
  }
  if (!force && pageLoaded.ecoslots) return;
  await refreshEcoslots(false);
  pageLoaded.ecoslots = true;
  pageLoaded.settings = true;
}

async function refreshControl() {
  const key = getKey();
  const el = document.getElementById("controlStatus");
  if (!key) { el.textContent = "Ustaw klucz API powyżej, żeby zobaczyć i zmieniać status."; return; }
  const r = await fetch("/api/guardian/control", { headers: { "X-Guardian-Api-Key": key } });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { el.textContent = j.detail || "error"; return; }
  renderToggleStatus(el, j.control_enabled, j.source, {
    on: "Zapisy włączone",
    off: "Zapisy wyłączone",
  });
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
  renderToggleStatus(document.getElementById("controlStatus"), j.control_enabled, j.source, {
    on: "Zapisy włączone",
    off: "Zapisy wyłączone",
  });
}

async function refreshPlanner() {
  const key = getKey();
  const el = document.getElementById("plannerStatus");
  const hz = document.getElementById("plannerHorizon");
  if (!key) { el.textContent = "Ustaw klucz API powyżej, żeby zobaczyć i zmieniać status."; hz.textContent = ""; return; }
  const r = await fetch("/api/guardian/planner", { headers: { "X-Guardian-Api-Key": key } });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { el.textContent = j.detail || "error"; hz.textContent = ""; return; }
  renderToggleStatus(el, j.planner_execution_enabled, j.source, {
    on: "Plan stosowany w Guardianie",
    off: "Plan tylko podgląd (nie stosowany)",
  });
  if (!j.horizon_start) {
    hz.textContent = "Brak plan_latest.json — uruchom: uv run python -m planner plan";
    return;
  }
  let pol = "";
  if (j.policy_hours_count != null) {
    pol = ` · polityka ${j.policy_hours_count} h${j.policy_degraded ? " (degraded)" : ""}`;
    if (j.policy_valid_until) pol += `, ważna do ${j.policy_valid_until.slice(0, 19)}`;
  }
  hz.textContent = `Aktualny plan ${(j.plan_id || "").slice(0, 8)}… · ${j.horizon_start} → ${j.horizon_end}${pol}`;
}

async function putPlanner(enabled) {
  const key = getKey();
  if (!key) { alert("Ustaw klucz API"); return; }
  const r = await fetch("/api/guardian/planner", {
    method: "PUT",
    headers: { "Content-Type": "application/json", "X-Guardian-Api-Key": key },
    body: JSON.stringify({ planner_execution_enabled: enabled }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { alert(j.detail || "error"); return; }
  renderToggleStatus(document.getElementById("plannerStatus"), j.planner_execution_enabled, j.source, {
    on: "Plan stosowany w Guardianie",
    off: "Plan tylko podgląd (nie stosowany)",
  });
}

async function saveWatchdog() {
  const key = getKey(), st = document.getElementById("wdSaveStatus");
  if (!key) { st.textContent = "Ustaw klucz API"; return; }
  const eb = (window._lastWds || {}).env_base;
  if (!eb) { st.textContent = "Brak konfiguracji"; return; }
  const body = {};
  const nightEnabled = document.getElementById("wd_soc_night_reserve_enabled").checked;
  body.soc_night_reserve_enabled = nightEnabled === eb.soc_night_reserve_enabled ? null : nightEnabled;
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
  st.textContent = "Przywrócono wartości z .env (plik nadpisania usunięty).";
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
  if (!live && document.activeElement && document.activeElement.closest(".eco-slot-card")) {
    return;
  }
  const st = document.getElementById("ecoSlotsStatus");
  if (!pageLoaded.ecoslots || live) st.textContent = live ? "odczyt z inwertera…" : "ładowanie snapshot…";
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

/** Interwał auto-odświeżania aktywnej zakładki [ms]. */
const PAGE_POLL_MS = {
  overview: 15000,
  history: 30000,
  forecast: 60000,
  kpi: 60000,
  settings: 20000,
};

function startPagePolling(page) {
  const ms = PAGE_POLL_MS[page];
  if (!ms) return;
  pollTimer = setInterval(() => {
    const loader = PAGE_LOADERS[page];
    if (loader) loader(true);
  }, ms);
}

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
  startPagePolling(page);
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

document.getElementById("kpiDay").addEventListener("change", () => { pageLoaded.kpi = false; loadKpi(true); });
document.getElementById("saveKey").addEventListener("click", () => {
  localStorage.setItem("guardianApiKey", document.getElementById("apiKey").value.trim());
  refreshControl().catch(console.error);
  refreshPlanner().catch(console.error);
  updateEvChargingAuthHint();
});
document.getElementById("refreshControl").addEventListener("click", () => refreshControl().catch(console.error));
document.getElementById("enableControl").addEventListener("click", () => putControl(true));
document.getElementById("disableControl").addEventListener("click", () => putControl(false));
document.getElementById("refreshPlanner").addEventListener("click", () => refreshPlanner().catch(console.error));
document.getElementById("enablePlanner").addEventListener("click", () => putPlanner(true));
document.getElementById("disablePlanner").addEventListener("click", () => putPlanner(false));
document.getElementById("saveWatchdog").addEventListener("click", () => saveWatchdog().catch(console.error));
document.getElementById("resetWatchdog").addEventListener("click", () => resetWatchdog().catch(console.error));
document.getElementById("wd_soc_night_reserve_enabled").addEventListener("change", (e) => {
  setNightReserveFieldsEnabled(e.target.checked);
});

document.getElementById("evChargingSave").addEventListener("click", () => saveEvChargingPlan().catch(console.error));
document.getElementById("evChargingClear").addEventListener("click", () => clearEvChargingPlan().catch(console.error));

const ecoPanels = document.getElementById("ecoSlotsPanels");
ecoPanels.addEventListener("click", handleEcoSaveEvent);

document.getElementById("apiKey").value = getKey();
initKpiDayPicker();
if (!location.hash) location.hash = "overview";
navigate(parsePageFromHash(), true);