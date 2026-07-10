function dv(v, d) { return v !== null && v !== undefined ? v : d; }
const fmt = (v) => (v === null || v === undefined) ? "—" : v;
function card(key, val) {
  return `<div class="card"><div class="k">${key}</div><div class="v">${fmt(val)}</div></div>`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/"/g, "&quot;");
}

function fmtW(w) {
  if (w === null || w === undefined) return "—";
  const n = Number(w);
  if (Number.isNaN(n)) return String(w);
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(2)} kW`;
  return `${n.toFixed(0)} W`;
}

function fmtPowerKw(kw) {
  if (kw === null || kw === undefined) return "—";
  const n = Number(kw);
  return Number.isNaN(n) ? String(kw) : `${n.toFixed(2)} kW`;
}

function fmtTimeLeft(sec) {
  if (sec === null || sec === undefined) return "—";
  const n = Math.round(Number(sec));
  if (Number.isNaN(n)) return String(sec);
  if (n < 60) return `${n} s`;
  const m = Math.floor(n / 60);
  const s = n % 60;
  return s ? `${m} min ${s} s` : `${m} min`;
}

function statusMetric(label, value, extraClass = "", title = "") {
  const t = title ? ` title="${escapeHtml(title)}"` : "";
  return `<div class="status-metric ${extraClass}"${t}><div class="label">${label}</div><div class="val">${value}</div></div>`;
}

function signedMetricClass(n, invert) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "";
  const v = Number(n);
  if (Math.abs(v) < 1e-6) return "";
  const pos = invert ? v < 0 : v > 0;
  return pos ? "val-pos" : "val-neg";
}

function formatGuardianReason(reason) {
  if (reason === null || reason === undefined || reason === "—") return escapeHtml(fmt(reason));
  let base = String(reason).trim();
  if (!base) return "—";

  let controlOff = null;
  const controlMatch = base.match(/\s+\[control_off:([^\]]+)\]\s*$/);
  if (controlMatch) {
    controlOff = controlMatch[1];
    base = base.slice(0, controlMatch.index);
  }

  const planMatch = base.match(/\s+\[plan_exec:([^\]]+)\]\s*$/);
  if (!planMatch) return escapeHtml(base);

  const decision = base.slice(0, planMatch.index).trim();
  const inside = planMatch[1];
  const lines = [];

  if (decision) lines.push(`<span class="reason-decision">${escapeHtml(decision)}</span>`);

  if (/\bno_policy\b/.test(inside)) {
    const src = inside.replace(/\s*no_policy\s*/g, " ").trim();
    if (src) lines.push(`<span class="reason-meta">plan_exec: ${escapeHtml(src)}</span>`);
    lines.push(`<span class="reason-meta">brak policy</span>`);
  } else {
    const execSrc = inside.match(/^(\S+)/)?.[1];
    const mode = inside.match(/\bmode=(\S+)/)?.[1];
    const target = inside.match(/\btarget_net=([+-]?\d+(?:\.\d+)?)/)?.[1];
    const actual = inside.match(/\bactual_net=([+-]?\d+(?:\.\d+)?)/)?.[1];
    if (execSrc) lines.push(`<span class="reason-meta">plan_exec: ${escapeHtml(execSrc)}</span>`);
    if (mode) lines.push(`<span class="reason-meta">mode: ${escapeHtml(mode)}</span>`);
    if (target != null && actual != null) {
      lines.push(`<span class="reason-net">net ${escapeHtml(actual)} → ${escapeHtml(target)} kWh</span>`);
    }
  }

  if (controlOff) {
    lines.push(`<span class="reason-meta">control off: ${escapeHtml(controlOff)}</span>`);
  }

  return lines.join("<br>") || escapeHtml(base);
}

function renderStatus(f) {
  const el = document.getElementById("statusBlock");
  if (!el) return;
  const reasonRaw = f.reason;
  const reasonHtml = formatGuardianReason(reasonRaw);
  const reasonTitle = reasonRaw != null && reasonRaw !== "" ? String(reasonRaw) : "—";
  const intervene = f.intervene === true || f.intervene === "true";
  const cmdOn = f.cmd_enabled === true || f.cmd_enabled === "true";
  const soc = f.soc_pct != null && !Number.isNaN(Number(f.soc_pct)) ? `${Number(f.soc_pct).toFixed(0)} %` : "—";
  const balKwh = f.remaining_kwh != null && !Number.isNaN(Number(f.remaining_kwh))
    ? `${Number(f.remaining_kwh).toFixed(2)} kWh` : "—";
  const cmdLabel = cmdOn
    ? `On ${fmt(f.cmd_pct)} % · ${fmt(f.cmd_duration_s)} s`
    : "wyłączone";

  el.innerHTML =
    `<div class="status-head">` +
    `<span class="status-ts">Odczyt: ${fmt(f.ts)}</span>` +
    `<span class="status-pill ${intervene ? "status-on" : "status-off"}">${intervene ? "interwencja" : "auto"}</span>` +
    `</div>` +
    `<div class="status-cols">` +
    `<div class="status-col">` +
    `<h4>Energia</h4>` +
    `<div class="status-nums">` +
    statusMetric("PV", fmtPowerKw(f.pv_kw)) +
    statusMetric("Dom", fmtW(f.house_w)) +
    statusMetric("Sieć", fmtPowerKw(f.grid_kw), signedMetricClass(f.grid_kw)) +
    statusMetric(
      "Bilans godz.",
      balKwh,
      signedMetricClass(f.remaining_kwh),
      "Δeksport − Δimport w bieżącej godzinie (kWh)"
    ) +
    `</div></div>` +
    `<div class="status-col">` +
    `<h4>Bateria</h4>` +
    `<div class="status-nums">` +
    statusMetric("SOC", soc, "hero") +
    statusMetric("Moc", fmtW(f.p_bat_w), signedMetricClass(f.p_bat_w, true)) +
    statusMetric(
      "Moc domknięcia",
      fmtPowerKw(f.balancing_kw),
      signedMetricClass(f.balancing_kw),
      "Średnia moc do wyzerowania bilansu do końca godziny; znak jak bilans (+ = eksport)"
    ) +
    statusMetric("Eco slot", f.ecoslot_read_pct != null ? `${fmt(f.ecoslot_read_pct)} %` : "—") +
    `</div></div>` +
    `<div class="status-col status-col-wide">` +
    `<h4>Guardian</h4>` +
    `<div class="status-reason" title="${escapeHtml(reasonTitle)}">${reasonHtml}</div>` +
    `<div class="status-nums status-nums-inline">` +
    statusMetric("Do końca slotu", fmtTimeLeft(f.time_to_end_s)) +
    statusMetric("Polecenie", cmdLabel) +
    `</div></div>` +
    `</div>`;
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
    const [status, pyramid, planViz] = await Promise.all([
      fetchJson("/api/status", 10000),
      fetchJson("/api/pv-pyramid", 60000).catch((e) => ({ _error: String(e) })),
      fetchJson("/api/plan/visualization", 60000).catch((e) => ({ _error: String(e) })),
    ]);
    document.getElementById("logPath").textContent = status.log_path || "—";
    renderStatus(status.fields || {});
    renderPlanTimeline(planViz);
    renderPvPyramid(pyramid);
    pageLoaded.overview = true;
    setUpdated(true);
  } catch (e) {
    setUpdated(false);
    console.error(e);
  }
}

const PLAN_MODE_SHORT = {
  export_pv_surplus: "PV",
  export_profit: "EXP",
  neutral: "0",
  import_grid: "IMP",
  charge_grid: "ŁAD",
};

const PLAN_MODE_LEGEND = [
  ["mode-export_pv_surplus", "eksport PV"],
  ["mode-export_profit", "eksport zarobkowy"],
  ["mode-neutral", "neutralny"],
  ["mode-import_grid", "import z sieci"],
  ["mode-charge_grid", "ładowanie z sieci"],
];

const PLAN_RCE_CHEAP = 0.60;

function planHourTooltip(h) {
  const parts = [
    `Godz. ${String(h.hour).padStart(2, "0")}:00`,
    h.exec_mode_label ? `Tryb: ${h.exec_mode_label}` : null,
    h.target_net_kwh != null ? `Net plan: ${Number(h.target_net_kwh).toFixed(2)} kWh` : null,
    h.soc_end_pct != null ? `SOC koniec: ${Number(h.soc_end_pct).toFixed(0)} %` : null,
    h.sell_pln_kwh != null ? `RCE: ${Number(h.sell_pln_kwh).toFixed(3)} PLN/kWh` : null,
    h.pv_kwh != null ? `PV: ${Number(h.pv_kwh).toFixed(2)} kWh` : null,
    h.load_kwh_p50 != null ? `Load p50: ${Number(h.load_kwh_p50).toFixed(2)} kWh` : null,
  ].filter(Boolean);
  return parts.join(" · ");
}

function renderPlanSocSvg(hours, svgId) {
  const pts = hours
    .map((h, i) => ({ x: i, y: h.soc_end_pct != null ? Number(h.soc_end_pct) : null }))
    .filter((p) => p.y != null && !Number.isNaN(p.y));
  if (pts.length < 2) {
    return `<svg class="plan-soc-chart" id="${svgId}" viewBox="0 0 100 72" preserveAspectRatio="none"></svg>`;
  }
  const ys = pts.map((p) => p.y);
  const minY = Math.max(0, Math.min(...ys) - 5);
  const maxY = Math.min(100, Math.max(...ys) + 5);
  const span = maxY - minY || 1;
  const w = 100;
  const h = 72;
  const toX = (i) => (i / 23) * w;
  const toY = (y) => h - ((y - minY) / span) * (h - 8) - 4;
  const linePts = hours.map((hr, i) => {
    const y = hr.soc_end_pct != null ? Number(hr.soc_end_pct) : null;
    if (y == null || Number.isNaN(y)) return null;
    return `${toX(i).toFixed(1)},${toY(y).toFixed(1)}`;
  }).filter(Boolean);
  const area = linePts.length
    ? `M ${toX(0).toFixed(1)},${h} L ${linePts.join(" L ")} L ${toX(23).toFixed(1)},${h} Z`
    : "";
  return (
    `<svg class="plan-soc-chart" id="${svgId}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">` +
    (area ? `<path class="area" d="${area}"/>` : "") +
    `<path class="line" d="M ${linePts.join(" L ")}"/>` +
    `</svg>`
  );
}

function renderPlanHourCell(h) {
  const mode = h.exec_mode ? String(h.exec_mode) : "";
  const cls = [
    "plan-hour-cell",
    h.hour_complete ? "past" : "",
    h.is_now ? "now" : "",
    mode ? "" : "empty",
    h.sell_pln_kwh != null && Number(h.sell_pln_kwh) < PLAN_RCE_CHEAP ? "rce-cheap" : "",
  ].filter(Boolean).join(" ");
  const short = mode ? (PLAN_MODE_SHORT[mode] || mode.slice(0, 3)) : "—";
  const net = h.target_net_kwh != null ? Number(h.target_net_kwh) : null;
  let netHtml;
  if (net != null && !Number.isNaN(net)) {
    const netCls = net > 0.02 ? "pos" : net < -0.02 ? "neg" : "";
    netHtml = `<div class="plan-hour-net ${netCls}">${net >= 0 ? "+" : ""}${net.toFixed(1)}</div>`;
  } else {
    netHtml = `<div class="plan-hour-net">—</div>`;
  }
  const modeCls = mode ? `mode-${mode}` : "";
  return (
    `<div class="${cls}" title="${escapeHtml(planHourTooltip(h))}">` +
    `<div class="plan-hour-mode ${modeCls}">${short}</div>` +
    netHtml +
    `<div class="plan-hour-label">${String(h.hour).padStart(2, "0")}</div>` +
    `</div>`
  );
}

function renderPlanDayBoard(day, dimmed) {
  const hours = day.hours || [];
  const date = String(day.date || "");
  const label = String(day.label || date);
  const cells = hours.map((h) => renderPlanHourCell(h)).join("");
  const svgId = `planSoc-${date.replace(/-/g, "")}`;
  return (
    `<div class="plan-day-board${dimmed ? " dimmed" : ""}">` +
    `<h4>${label} <span class="muted" style="font-weight:400;font-size:11px;">${date.slice(5)}</span></h4>` +
    `<div class="plan-hour-grid">${cells}</div>` +
    renderPlanSocSvg(hours, svgId) +
    `</div>`
  );
}

function renderPlanTimeline(p) {
  const block = document.getElementById("planTimelineBlock");
  const content = document.getElementById("planTimelineContent");
  if (!block || !content) return;
  if (!p || p._error || !p.available) {
    block.style.display = "none";
    return;
  }
  block.style.display = "block";
  const meta = p.meta || {};
  const policy = meta.policy || {};
  const execOn = Boolean(meta.execution_enabled);
  const cash = meta.expected_cashflow_pln != null
    ? `${Number(meta.expected_cashflow_pln).toFixed(2)} PLN` : "—";
  const socStart = meta.soc_start_pct != null ? `${Number(meta.soc_start_pct).toFixed(0)}%` : "—";
  const socEnd = meta.soc_end_pct != null ? `${Number(meta.soc_end_pct).toFixed(0)}%` : "—";
  const planId = meta.plan_id ? String(meta.plan_id).slice(0, 8) : "—";
  const validUntil = policy.valid_until ? String(policy.valid_until).slice(0, 19) : "—";
  const hero =
    `<div class="plan-hero">` +
    `<div><div class="plan-hero-key">Plan</div><div>${planId}… · ważny do ${validUntil}</div></div>` +
    `<div><div class="plan-hero-key">Σ cashflow</div><div class="plan-hero-val">${cash}</div></div>` +
    `<div><div class="plan-hero-key">SOC plan</div><div>${socStart} → ${socEnd}</div></div>` +
    `<span class="status-pill ${execOn ? "status-on" : "status-off"}">egzekucja: ${execOn ? "tak" : "nie"}</span>` +
    `</div>`;
  const days = p.days || [];
  const tomorrowDim = !p.pricing_tomorrow_available;
  const boards = days.map((d, i) => renderPlanDayBoard(d, i === 1 && tomorrowDim)).join("");
  const legend = PLAN_MODE_LEGEND.map(([cls, label]) =>
    `<span><i class="plan-hour-mode ${cls}"></i>${label}</span>`
  ).join("");
  content.innerHTML =
    hero +
    `<div class="plan-day-boards">${boards}</div>` +
    `<div class="plan-legend">${legend}</div>` +
    (tomorrowDim ? `<p class="muted" style="font-size:11px;margin:8px 0 0;">Jutro: RCE jeszcze nieopublikowane — panel przygaszony.</p>` : "");
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
    const pct = Math.min(100, Math.round((cum / barMax) * 100));
    const cls = gr <= 30 ? "pyramid-cheap" : gr <= 50 ? "pyramid-mid" : "";
    return `<tr class="${cls}"><td>&lt;${gr} gr</td><td>${cum.toFixed(1)}</td>
      <td class="pv-bar-wrap"><span class="pv-bar" style="width:${pct}%;"></span></td></tr>`;
  }).join("");
  const abovePct = Math.min(100, Math.round((above / barMax) * 100));
  tbody.innerHTML = tierRows +
    `<tr><td>≥60 gr</td><td>${above.toFixed(1)}</td>
      <td class="pv-bar-wrap"><span class="pv-bar" style="width:${abovePct}%; opacity:0.55;"></span></td></tr>`;
}

function renderPvPyramidNums(segment, elId, cheapGr) {
  const el = document.getElementById(elId);
  if (!el) return;
  const cheap = Number(segment?.cheap_kwh || 0);
  const surplus = Number(segment?.cheap_surplus_kwh || 0);
  el.innerHTML =
    `<div class="pv-pyramid-num"><div class="label">PV tanio (&lt;${cheapGr} gr)</div>` +
    `<div class="val">${cheap.toFixed(1)} kWh</div></div>` +
    `<div class="pv-pyramid-num net"><div class="label">Po load (nadwyżka)</div>` +
    `<div class="val">${surplus.toFixed(1)} kWh</div></div>`;
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
  const remaining = today.remaining || {};
  const tomorrowTotal = tomorrow.total || {};
  const cheapGr = seg.cheap_threshold_gr || 60;

  renderPvPyramidNums(remaining, "pvPyramidTodayNums", cheapGr);
  renderPvPyramidNums(tomorrowTotal, "pvPyramidTomorrowNums", cheapGr);
  renderPvPyramidTable(remaining, "pvPyramidRowsToday");
  renderPvPyramidTable(tomorrowTotal, "pvPyramidRowsTomorrow");

  const tomorrowCol = document.getElementById("pvPyramidTomorrowCol");
  if (tomorrowCol) {
    tomorrowCol.style.opacity = p.pricing_tomorrow_available ? "1" : "0.45";
  }

  const meta = [
    p.pricing_tomorrow_available ? `jutro RCE: ${p.pricing_tomorrow_source || "ok"}` : "jutro RCE: jeszcze nieopublikowane",
    `dziś zostało: ${remaining.hours_with_pv || 0} h z PV`,
    `jutro: ${tomorrowTotal.hours_with_pv || 0} h z PV`,
  ];
  document.getElementById("pvPyramidMeta").textContent = meta.join(" · ");
  const warns = p.warnings || [];
  document.getElementById("pvPyramidWarnings").textContent = warns.length
    ? `Uwagi: ${warns.slice(0, 4).join(" · ")}` : "";
}

async function loadHistory(force) {
  if (!force && pageLoaded.history) return;
  const st = document.getElementById("historyStatus");
  if (!pageLoaded.history) st.textContent = "ładowanie…";
  else if (force) st.textContent = "odświeżanie…";
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

function renderEvChargingPanel(ev) {
  if (!ev) return;
  const budget = ev.cheap_budget || {};
  const cheapExport = Number(budget.cheap_export_kwh || 0);
  const cheapImp = Number(budget.cheap_import_kwh || 0);
  const rec = Number(budget.recommendable_kwh || 0);
  const hero = document.getElementById("evChargingHero");
  if (hero) {
    hero.innerHTML =
      `<div class="card"><div class="card-key">Eksport tanio (&lt;60 gr)</div><div class="card-val">${cheapExport.toFixed(1)} kWh</div></div>` +
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
  const futureSlots = decl ? (ev.slots || []) : (ev.recommended_slots || []);
  const pastSlots = decl ? (ev.past_slots || []) : [];
  const delivered = decl ? Number(ev.delivered_kwh || 0) : 0;
  const remaining = decl ? Number(ev.remaining_kwh ?? ev.declaration?.target_kwh ?? 0) : 0;
  if (slotsEl) {
    const parts = [];
    if (pastSlots.length) {
      parts.push(
        "Już naładowano: " + pastSlots.map((s) => `${String(s.hour).padStart(2, "0")}:00 → ${Number(s.kwh).toFixed(1)} kWh`).join(", ")
      );
    }
    if (futureSlots.length) {
      const prefix = decl ? "Plan na przyszłość" : "Propozycja slotów";
      parts.push(
        prefix + ": " + futureSlots.map((s) => `${String(s.hour).padStart(2, "0")}:00 → ${Number(s.kwh).toFixed(1)} kWh`).join(", ")
      );
    }
    if (decl && delivered > 0.001) {
      parts.push(`Pozostało do zaplanowania: ${remaining.toFixed(1)} kWh (cel ${Number(decl.target_kwh).toFixed(1)} kWh)`);
    }
    if (!parts.length) {
      slotsEl.textContent = decl
        ? (delivered > 0.001 && remaining < 0.001
          ? `Cel ${Number(decl.target_kwh).toFixed(1)} kWh już zrealizowany dziś.`
          : "Brak przypisanych godzin — zapisz plan ponownie.")
        : "Brak deklaracji — podaj cel kWh i zapisz (propozycja slotów pojawi się po zapisie lub w rekomendacji).";
    } else {
      slotsEl.textContent = parts.join(" · ");
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

function pvCorrMetric(label, value, extraClass = "", title = "") {
  const t = title ? ` title="${escapeHtml(title)}"` : "";
  return `<div class="pv-correction-metric ${extraClass}"${t}><div class="label">${label}</div><div class="val">${value}</div></div>`;
}

function fmtKwh(v, d = 3) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return `${Number(v).toFixed(d)} kWh`;
}

function renderPvCorrectionChart(curve, alpha) {
  const svg = document.getElementById("pvCorrectionChart");
  if (!svg || !curve || !curve.length) {
    if (svg) svg.innerHTML = "";
    return;
  }
  const w = 600;
  const h = 220;
  const pad = { l: 36, r: 12, t: 12, b: 28 };
  const innerW = w - pad.l - pad.r;
  const innerH = h - pad.t - pad.b;
  const ymax = Math.max(
    0.05,
    ...curve.map((p) => Math.max(p.solcast_kwh || 0, p.actual_kwh || 0, p.plan_kwh || 0))
  );
  const x = (m) => pad.l + (m / 60) * innerW;
  const y = (v) => pad.t + innerH - (v / ymax) * innerH;
  const linePath = (key) => {
    const pts = curve.filter((p) => p[key] != null);
    if (!pts.length) return "";
    return pts.map((p, i) => `${i ? "L" : "M"}${x(p.minute).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  };
  const nowX = x(Math.min(60, Math.max(0, alpha * 60)));
  svg.innerHTML =
    `<rect x="0" y="0" width="${w}" height="${h}" fill="transparent"/>` +
    `<text x="${pad.l}" y="${h - 6}" font-size="10" fill="currentColor" opacity="0.6">:00</text>` +
    `<text x="${w - pad.r - 16}" y="${h - 6}" font-size="10" fill="currentColor" opacity="0.6">:60</text>` +
    `<path class="line-solcast" d="${linePath("solcast_kwh")}"/>` +
    `<path class="line-actual" d="${linePath("actual_kwh")}"/>` +
    `<path class="line-plan" d="${linePath("plan_kwh")}"/>` +
    `<line class="now-v" x1="${nowX.toFixed(1)}" y1="${pad.t}" x2="${nowX.toFixed(1)}" y2="${h - pad.b}"/>` +
    `<text x="${Math.min(w - 40, nowX + 4)}" y="${pad.t + 10}" font-size="10" fill="currentColor">teraz</text>`;
}

function renderPvCorrectionBars(projections) {
  const el = document.getElementById("pvCorrectionBars");
  if (!el || !projections) return;
  const items = [
    ["Solcast p50", projections.solcast_full_hour_kwh, "base"],
    ["k_intra only", projections.k_intra_only_kwh, "alt"],
    ["rate only", projections.rate_only_kwh, "alt"],
    ["Plan finalny", projections.final_plan_kwh, ""],
  ].filter(([, v]) => v != null);
  const max = Math.max(0.05, ...items.map(([, v]) => Number(v)));
  el.innerHTML = items.map(([label, val, cls]) => {
    const pct = Math.max(2, (Number(val) / max) * 100);
    return `<div class="pv-correction-bar-item"><div class="bar-label">${escapeHtml(label)} · ${fmtKwh(val, 2)}</div>` +
      `<div class="pv-correction-bar"><span class="${cls || ""}" style="width:${pct.toFixed(0)}%"></span></div></div>`;
  }).join("");
}

function renderPvCorrectionBlock(payload) {
  const c = payload.correction || {};
  const p = payload.projections || {};
  const alphaPct = c.alpha != null ? `${(Number(c.alpha) * 100).toFixed(0)}%` : "—";
  const kRaw = c.k_raw != null ? Number(c.k_raw).toFixed(3) : "—";
  const kIntra = c.k_intra != null ? Number(c.k_intra).toFixed(3) : "—";
  const clipNow = c.clip_min_effective != null
    ? `[${Number(c.clip_min_effective).toFixed(2)}, ${Number(c.clip_max_effective).toFixed(2)}]`
    : "—";

  document.getElementById("pvCorrectionMetrics").innerHTML =
    pvCorrMetric("Godzina", `${payload.current_hour}:00`, "hero") +
    pvCorrMetric("α (minuta)", alphaPct) +
    pvCorrMetric("A_so_far", fmtKwh(c.a_so_far_kwh, 3)) +
    pvCorrMetric("F50 Solcast", fmtKwh(c.f50_current_kwh, 2)) +
    pvCorrMetric("k_raw", kRaw) +
    pvCorrMetric("k_intra", kIntra) +
    pvCorrMetric("Clip efektywny", clipNow, "", "Dynamiczne granice clipu zależne od α") +
    pvCorrMetric("recent kW", c.recent_kw != null ? `${Number(c.recent_kw).toFixed(2)} kW` : "—") +
    pvCorrMetric("Plan h", fmtKwh(p.final_plan_kwh, 2), "hero") +
    pvCorrMetric("Plan h+1", fmtKwh(c.pv_plan_next_kwh, 2)) +
    pvCorrMetric("Metoda", fmt(c.plan_method || c.reason));

  renderPvCorrectionChart(payload.projection_curve || [], Number(c.alpha || 0));
  renderPvCorrectionBars(p);

  const clipRows = document.getElementById("pvCorrectionClipRows");
  if (clipRows) {
    clipRows.innerHTML = (payload.clip_timeline || []).map((row) =>
      `<tr><td>${Number(row.alpha).toFixed(2)}</td><td>${Number(row.clip_min).toFixed(3)}</td><td>${Number(row.clip_max).toFixed(3)}</td></tr>`
    ).join("");
  }
  const clipNowEl = document.getElementById("pvCorrectionClipNow");
  if (clipNowEl) {
    const w = c.dynamic_clip_weight != null ? (Number(c.dynamic_clip_weight) * 100).toFixed(0) : "0";
    clipNowEl.textContent = c.dynamic_clip_enabled
      ? `Teraz: w=${w}% szerokiego clipu · k_raw=${kRaw} → k_intra=${kIntra}`
      : "Dynamiczny clip wyłączony — stały clip 0.65–1.35";
  }

  const dayRows = document.getElementById("pvCorrectionDayRows");
  if (dayRows) {
    dayRows.innerHTML = (payload.today_hours || []).map((row) => {
      const cls = row.in_progress ? "in-progress" : (row.complete ? "complete" : "");
      const actual = row.complete
        ? fmtKwh(row.actual_kwh, 2)
        : (row.in_progress ? fmtKwh(row.actual_so_far_kwh, 3) + "*" : "—");
      const plan = row.pv_plan_kwh != null ? fmtKwh(row.pv_plan_kwh, 2) : "—";
      let delta = "—";
      if (row.delta_kwh != null) {
        const d = Number(row.delta_kwh);
        delta = (d >= 0 ? "+" : "") + d.toFixed(2);
      } else if (row.delta_so_far_kwh != null) {
        const d = Number(row.delta_so_far_kwh);
        delta = (d >= 0 ? "+" : "") + d.toFixed(2) + "*";
      }
      return `<tr class="${cls}"><td>${String(row.hour).padStart(2, "0")}</td>` +
        `<td>${fmtKwh(row.f50_kwh, 2)}</td><td>${actual}</td><td>${plan}</td><td>${delta}</td></tr>`;
    }).join("");
  }

  const meta = document.getElementById("pvCorrectionMeta");
  if (meta) {
    meta.textContent = [
      `updated: ${payload.now || "—"}`,
      c.enabled ? "correction on" : "correction off",
      c.source_current ? `source: ${c.source_current}` : "",
      p.remaining_kwh != null ? `reszta h: ${Number(p.remaining_kwh).toFixed(3)} kWh` : "",
      c.rate_blend_weight ? `rate blend w=${(Number(c.rate_blend_weight) * 100).toFixed(0)}%` : "",
    ].filter(Boolean).join(" · ");
  }
}

async function loadPvCorrection(force) {
  if (!force && pageLoaded["pv-correction"]) return;
  const st = document.getElementById("pvCorrectionStatus");
  if (!pageLoaded["pv-correction"] && st) st.textContent = "ładowanie…";
  try {
    const payload = await fetchJson("/api/pv-correction", 15000);
    renderPvCorrectionBlock(payload);
    pageLoaded["pv-correction"] = true;
    if (st) st.textContent = "OK";
    setUpdated(true);
  } catch (e) {
    if (st) st.textContent = String(e);
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
  pageLoaded.settings = true;
}

async function loadSlots(force) {
  if (!force && pageLoaded.slots) return;
  await refreshEcoslots(false);
  pageLoaded.slots = true;
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
  if (!pageLoaded.slots || live) st.textContent = live ? "odczyt z inwertera…" : "ładowanie snapshot…";
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
    alert("Ustaw klucz API w zakładce Ustawienia (ten sam co na laptopie).");
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
  "pv-correction": loadPvCorrection,
  kpi: loadKpi,
  slots: loadSlots,
  settings: loadSettings,
};

/** Interwał auto-odświeżania aktywnej zakładki [ms]. */
const PAGE_POLL_MS = {
  overview: 15000,
  history: 15000,
  forecast: 60000,
  "pv-correction": 15000,
  kpi: 60000,
  slots: 20000,
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
  const switching = currentPage !== page;
  if (currentPage === page && !force) return;
  currentPage = page;
  document.querySelectorAll(".page").forEach((el) => el.classList.remove("active"));
  document.getElementById("page-" + page).classList.add("active");
  document.querySelectorAll("#mainNav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === page);
  });
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  // Przy przełączeniu zakładki zawsze pobierz świeże dane (pageLoaded blokował ponowne wejście).
  PAGE_LOADERS[page](!!force || switching);
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