// Session timeline page: per-call token use for one session and its subagents.
// Data comes from web/sessions/<id>.js (written by token_stats.py), loaded with a <script> tag so
// the page also works over file://. URL: session.html?d=<detail id>&s=<session id to focus>.
(() => {
  "use strict";

  const T = window.TokenObs;
  const { fmtTok, fmtUsd, esc } = T;
  let PALETTE = T.PALETTES.dark;

  // Call row columns (see "call_columns" in the detail file)
  const K_DT = 0, K_AGENT = 1, K_MODEL = 2, K_IN = 3, K_CR = 4, K_CW = 5, K_OUT = 6, K_REAS = 7;

  const TOP_AGENTS = 10;
  const PAD_L = 160; // shared left gutter so every chart's time axis lines up
  const PAD_R = 16;
  const NICE_STEPS = [
    1e3, 2e3, 5e3, 10e3, 15e3, 30e3,
    60e3, 2 * 60e3, 5 * 60e3, 10 * 60e3, 15 * 60e3, 30 * 60e3,
    3600e3, 2 * 3600e3, 3 * 3600e3, 6 * 3600e3, 12 * 3600e3, 86400e3, 2 * 86400e3, 7 * 86400e3,
  ];

  const params = new URLSearchParams(window.location.search);
  const detailId = params.get("d") || "";
  const focusParam = params.get("s") || "";

  let D = null;
  let customRates = T.loadSavedRates();
  let fullWin = [0, 1];
  let agentColor = [];
  let agentInTop = [];
  let agentParentIdx = [];
  let agentModels = []; // per agent: [[model, calls], ...] over the whole session, most-used first
  let callEst = null; // Float64Array of est $ per call (recomputed when rates change)
  let brushing = false; // a drag-to-zoom is in progress
  let lastBrushAt = 0; // when the last drag-to-zoom finished (so it isn't also treated as a click)

  const state = {
    metric: "total",
    stack: "agent",
    mode: "interval",
    win: null, // [loDt, hiDt] in ms relative to D.t0; null = whole session
    focus: null, // agent index or null
  };

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function rateFor(model) {
    return (customRates && customRates[model]) || (D.rates && D.rates[model]) || { in: 2.0, cr: 0.2, cw: 2.5, out: 10.0 };
  }

  function computeEst() {
    callEst = new Float64Array(D.calls.length);
    D.calls.forEach((c, i) => {
      const r = rateFor(D.models[c[K_MODEL]]);
      callEst[i] = (c[K_IN] * (r.in || 0) + c[K_CR] * (r.cr || 0) + c[K_CW] * (r.cw || 0) + (c[K_OUT] + c[K_REAS]) * (r.out || 0)) / 1e6;
    });
  }

  function metricOf(i, metric = state.metric) {
    const c = D.calls[i];
    if (metric === "active") return c[K_IN] + c[K_CW] + c[K_OUT] + c[K_REAS];
    if (metric === "generated") return c[K_OUT] + c[K_REAS];
    if (metric === "est_cost") return callEst[i];
    return c[K_IN] + c[K_CR] + c[K_CW] + c[K_OUT] + c[K_REAS];
  }

  function fmtMetric(v, metric = state.metric) {
    return metric === "est_cost" ? fmtUsd(v) : fmtTok(v);
  }

  function promptOf(c) {
    return c[K_IN] + c[K_CR] + c[K_CW];
  }

  const pad2 = (n) => String(n).padStart(2, "0");

  function absDate(dt) {
    return new Date(D.t0 + dt);
  }

  function fmtClock(dt, withSeconds = false, withDate = false) {
    const d = absDate(dt);
    const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}${withSeconds ? ":" + pad2(d.getSeconds()) : ""}`;
    return withDate ? `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${hm}` : hm;
  }

  function fmtDateTime(dt) {
    const d = absDate(dt);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${fmtClock(dt, true)}`;
  }

  function fmtDuration(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${pad2(s % 60)}s`;
    const h = Math.floor(m / 60);
    if (h < 48) return `${h}h ${pad2(m % 60)}m`;
    return `${Math.floor(h / 24)}d ${h % 24}h`;
  }

  function sameDay(a, b) {
    const x = absDate(a), y = absDate(b);
    return x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate();
  }

  function agentLabel(i, max = 48) {
    const a = D.agents[i];
    const base = i === 0 ? `Main · ${a.title}` : `Sub ${i} · ${a.title}`;
    return base.length > max ? base.slice(0, max - 1) + "…" : base;
  }

  function win() {
    return state.win || fullWin;
  }

  function inWin(dt) {
    const [lo, hi] = win();
    return dt >= lo && dt <= hi;
  }

  function positionTooltip(tooltip, ev) {
    tooltip.hidden = false;
    const tw = tooltip.offsetWidth || 250;
    const th = tooltip.offsetHeight || 140;
    tooltip.style.left = Math.max(12, Math.min(window.innerWidth - tw - 12, ev.clientX + 14)) + "px";
    tooltip.style.top = Math.max(12, Math.min(window.innerHeight - th - 12, ev.clientY - 24)) + "px";
  }

  // Time axis: ticks on clock-friendly steps, local time; dates shown when the window spans days
  function timeTicks(lo, hi, plotW) {
    const span = Math.max(1, hi - lo);
    const step = NICE_STEPS.find((s) => (s / span) * plotW >= 90) || NICE_STEPS[NICE_STEPS.length - 1];
    const tzOff = -new Date(D.t0 + lo).getTimezoneOffset() * 60000;
    const first = Math.ceil((D.t0 + lo + tzOff) / step) * step - tzOff - D.t0;
    const ticks = [];
    for (let t = first; t <= hi && ticks.length < 60; t += step) ticks.push(t);
    const multiDay = !sameDay(lo, hi);
    return ticks.map((t, i) => ({
      t,
      label: fmtClock(t, step < 60e3, multiDay && (i === 0 || !sameDay(ticks[i - 1], t))),
    }));
  }

  // Drag horizontally across a chart to zoom every chart to that time window
  function attachBrush(svg, width, x0, plotW, top, plotH, xToT) {
    let drag = null;
    const sel = svg.querySelector(".sel-rect");
    const tooltip = document.getElementById("chart-tooltip");
    const toX = (ev) => {
      const box = svg.getBoundingClientRect();
      return (ev.clientX - box.left) * (width / (box.width || 1));
    };
    svg.onpointerdown = (ev) => {
      if (ev.button !== 0) return;
      const x = toX(ev);
      if (x < x0 || x > x0 + plotW) return;
      drag = { a: x, b: x };
      brushing = true;
      try {
        svg.setPointerCapture(ev.pointerId);
      } catch {}
    };
    svg.onpointermove = (ev) => {
      if (!drag) return;
      drag.b = Math.max(x0, Math.min(x0 + plotW, toX(ev)));
      const lo = Math.min(drag.a, drag.b), hi = Math.max(drag.a, drag.b);
      if (hi - lo < 3) return;
      sel.setAttribute("x", lo.toFixed(1));
      sel.setAttribute("width", (hi - lo).toFixed(1));
      sel.setAttribute("visibility", "visible");
      const tLo = xToT(lo), tHi = xToT(hi);
      tooltip.innerHTML = `<div class="tt-title"><span>${esc(fmtClock(tLo, true))} → ${esc(fmtClock(tHi, true))}</span><span>${esc(fmtDuration(tHi - tLo))}</span></div><div class="tt-row"><span>release to zoom</span></div>`;
      positionTooltip(tooltip, ev);
    };
    const finish = () => {
      if (!drag) return;
      const lo = Math.min(drag.a, drag.b), hi = Math.max(drag.a, drag.b);
      drag = null;
      brushing = false;
      sel.setAttribute("visibility", "hidden");
      if (hi - lo < 3) return;
      tooltip.hidden = true;
      lastBrushAt = Date.now();
      const tLo = xToT(lo), tHi = xToT(hi);
      if (tHi - tLo < 1000) return;
      state.win = [tLo, tHi];
      renderAll();
    };
    svg.onpointerup = finish;
    svg.onpointercancel = () => {
      drag = null;
      brushing = false;
      sel.setAttribute("visibility", "hidden");
      tooltip.hidden = true;
    };
    return `<rect class="sel-rect" x="0" y="${top}" width="0" height="${plotH}" visibility="hidden" />`;
  }

  // ---------------------------------------------------------------------------
  // Setup
  // ---------------------------------------------------------------------------

  function fail(msg) {
    const el = document.getElementById("sess-error");
    el.innerHTML = `${esc(msg)}<br/><br/><a class="btn-link" href="./index.html">← Back to the observatory</a>`;
    el.hidden = false;
    document.getElementById("sess-subtitle").textContent = "Session not available";
  }

  function prepare() {
    const n = D.agents.length;
    const totals = new Array(n).fill(0);
    for (const c of D.calls) totals[c[K_AGENT]] += c[K_IN] + c[K_CR] + c[K_CW] + c[K_OUT] + c[K_REAS];

    // Top agents by total tokens (the root always included) get palette colours, in order of
    // first appearance; the rest share the neutral "Other" colour and fold into "Other" stacks.
    const ranked = [...totals.keys()].sort((a, b) => totals[b] - totals[a]);
    const top = new Set(ranked.slice(0, TOP_AGENTS));
    if (!top.has(0)) {
      top.delete(ranked[TOP_AGENTS - 1]);
      top.add(0);
    }
    agentInTop = [...Array(n).keys()].map((i) => top.has(i));
    agentColor = new Array(n).fill(PALETTE.other);
    let slot = 0;
    for (let i = 0; i < n; i++) {
      if (agentInTop[i]) agentColor[i] = PALETTE.series[slot++ % PALETTE.series.length];
    }

    const modelCounts = [...Array(n)].map(() => new Map());
    for (const c of D.calls) {
      const m = modelCounts[c[K_AGENT]];
      m.set(c[K_MODEL], (m.get(c[K_MODEL]) || 0) + 1);
    }
    agentModels = modelCounts.map((m) =>
      [...m.entries()].sort((a, b) => b[1] - a[1]).map(([mi, cnt]) => [D.models[mi], cnt])
    );

    const idxById = new Map(D.agents.map((a, i) => [a.id, i]));
    agentParentIdx = D.agents.map((a) => (a.parent && idxById.has(a.parent) ? idxById.get(a.parent) : -1));

    const last = D.calls.length ? D.calls[D.calls.length - 1][K_DT] : 0;
    const pad = Math.max(2000, last * 0.01);
    fullWin = [-pad, last + pad];
    computeEst();
  }

  // ---------------------------------------------------------------------------
  // Aggregates
  // ---------------------------------------------------------------------------

  function agentStats() {
    const n = D.agents.length;
    const st = [...Array(n).keys()].map((i) => ({
      idx: i,
      calls: 0, inp: 0, cr: 0, cw: 0, out: 0, reas: 0, est: 0,
      first: Infinity, last: -Infinity, peak: 0, compactions: 0,
      models: new Set(), prevPrompt: 0,
    }));
    D.calls.forEach((c, i) => {
      const s = st[c[K_AGENT]];
      const p = promptOf(c);
      const isDrop = s.prevPrompt > 20000 && p < s.prevPrompt * 0.6;
      s.prevPrompt = p;
      if (!inWin(c[K_DT])) return;
      s.calls++;
      s.inp += c[K_IN];
      s.cr += c[K_CR];
      s.cw += c[K_CW];
      s.out += c[K_OUT];
      s.reas += c[K_REAS];
      s.est += callEst[i];
      s.first = Math.min(s.first, c[K_DT]);
      s.last = Math.max(s.last, c[K_DT]);
      s.peak = Math.max(s.peak, p);
      if (isDrop) s.compactions++;
      s.models.add(D.models[c[K_MODEL]]);
    });
    for (const s of st) {
      s.total = s.inp + s.cr + s.cw + s.out + s.reas;
      s.active = s.inp + s.cw + s.out + s.reas;
      s.generated = s.out + s.reas;
    }
    return st;
  }

  // ---------------------------------------------------------------------------
  // Header, controls & KPIs
  // ---------------------------------------------------------------------------

  function renderHeader() {
    const first = D.calls.length ? D.calls[0][K_DT] : 0;
    const last = D.calls.length ? D.calls[D.calls.length - 1][K_DT] : 0;
    const hCol = PALETTE.harness[D.harness] || "var(--accent)";
    const subs = D.agents.length - 1;
    document.title = `${D.title.slice(0, 60)} · Session Timeline`;
    document.getElementById("sess-subtitle").textContent = `${D.harness} · ${D.project}`;
    document.getElementById("sess-title").textContent = D.title;
    document.getElementById("sess-meta").innerHTML = `
      <span class="badge"><span class="dot" style="background:${hCol}"></span>${esc(D.harness)}</span>
      <span>Project <strong>${esc(D.project)}</strong></span>
      <span><strong>${esc(fmtDateTime(first))}</strong> → <strong>${esc(sameDay(first, last) ? fmtClock(last, true) : fmtDateTime(last))}</strong></span>
      <span>Duration <strong>${esc(fmtDuration(last - first))}</strong></span>
      <span><strong>${subs}</strong> subagent${subs === 1 ? "" : "s"}</span>
      <span><strong>${D.calls.length.toLocaleString()}</strong> calls</span>
      <span class="mono" style="font-size:11.5px">${esc(D.models.join(", "))}</span>
      ${D.untimed_calls ? `<span>${D.untimed_calls.toLocaleString()} calls without timestamps not shown</span>` : ""}
    `;
  }

  function renderWindowControls() {
    const [lo, hi] = win();
    document.getElementById("window-label").textContent = state.win
      ? `${fmtClock(lo, true, !sameDay(lo, hi))} → ${fmtClock(hi, true, !sameDay(lo, hi))} (${fmtDuration(hi - lo)})`
      : "Whole session";
    document.getElementById("btn-reset-zoom").hidden = !state.win;
    const chip = document.getElementById("focus-chip");
    if (state.focus === null) {
      chip.hidden = true;
      chip.innerHTML = "";
    } else {
      chip.hidden = false;
      chip.innerHTML = `<span class="dot" style="background:${agentColor[state.focus]}"></span><span class="focus-name">${esc(agentLabel(state.focus, 60))}</span><button title="Show all agents">✕</button>`;
      chip.querySelector("button").addEventListener("click", () => setFocus(null));
    }
  }

  function renderKPIs(stats) {
    const sel = state.focus === null ? stats : [stats[state.focus]];
    const sum = (k) => sel.reduce((s, x) => s + x[k], 0);
    const total = sum("total"), active = sum("active"), gen = sum("generated"), cr = sum("cr");
    const calls = sum("calls"), est = sum("est"), reas = sum("reas");
    const peak = Math.max(0, ...sel.map((x) => x.peak));
    const comp = sum("compactions");
    const activeAgents = stats.filter((x) => x.calls > 0).length;
    const totIn = sum("inp") + cr + sum("cw");
    document.getElementById("kpi-grid").innerHTML = `
      <div class="kpi-card">
        <div class="kpi-label"><span>Total Tokens</span><span>All Buckets</span></div>
        <div class="kpi-value">${fmtTok(total)}</div>
        <div class="kpi-sub">${fmtTok(cr)} cached · <strong>${fmtTok(active)}</strong> fresh/active</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><span>Generated</span><span>Output</span></div>
        <div class="kpi-value">${fmtTok(gen)}</div>
        <div class="kpi-sub">${fmtTok(reas)} reasoning</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><span>Est. API Value</span><span>List rates</span></div>
        <div class="kpi-value">${fmtUsd(est)}</div>
        <div class="kpi-sub">cache hit ${totIn > 0 ? ((cr / totIn) * 100).toFixed(1) : "0.0"}%</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><span>Calls</span><span>LLM requests</span></div>
        <div class="kpi-value">${calls.toLocaleString()}</div>
        <div class="kpi-sub">${calls > 0 ? fmtTok(total / calls) : 0} tokens / call</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><span>Agents Active</span><span>In window</span></div>
        <div class="kpi-value">${activeAgents}</div>
        <div class="kpi-sub">main + ${Math.max(0, activeAgents - (stats[0].calls > 0 ? 1 : 0))} subagent(s)</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><span>Peak Context</span><span>Prompt size</span></div>
        <div class="kpi-value">${fmtTok(peak)}</div>
        <div class="kpi-sub">${comp} sharp drop${comp === 1 ? "" : "s"} (compactions)</div>
      </div>
    `;
  }

  // ---------------------------------------------------------------------------
  // Token burn chart
  // ---------------------------------------------------------------------------

  function bucketParts(i) {
    const c = D.calls[i];
    if (state.metric === "est_cost") {
      const r = rateFor(D.models[c[K_MODEL]]);
      return {
        "Cache Read": (c[K_CR] * (r.cr || 0)) / 1e6,
        "Cache Write": (c[K_CW] * (r.cw || 0)) / 1e6,
        "Uncached Input": (c[K_IN] * (r.in || 0)) / 1e6,
        "Text Output": (c[K_OUT] * (r.out || 0)) / 1e6,
        "Reasoning": (c[K_REAS] * (r.out || 0)) / 1e6,
      };
    }
    const all = {
      "Cache Read": c[K_CR],
      "Cache Write": c[K_CW],
      "Uncached Input": c[K_IN],
      "Text Output": c[K_OUT],
      "Reasoning": c[K_REAS],
    };
    if (state.metric === "active") delete all["Cache Read"];
    if (state.metric === "generated") {
      delete all["Cache Read"];
      delete all["Cache Write"];
      delete all["Uncached Input"];
    }
    return all;
  }

  function renderBurn() {
    const svg = document.getElementById("burn-svg");
    const legendEl = document.getElementById("burn-legend");
    const wrap = document.getElementById("burn-wrap");
    const tooltip = document.getElementById("chart-tooltip");
    const width = Math.max(360, wrap.clientWidth || 900);
    const height = Math.max(220, wrap.clientHeight || 290);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.classList.remove("series-focus");

    const padT = 14, padB = 30;
    const plotW = Math.max(100, width - PAD_L - PAD_R);
    const plotH = Math.max(100, height - padT - padB);
    const [lo, hi] = win();
    const span = Math.max(1, hi - lo);

    // Bin width: a clock-friendly step giving at most ~140 bars
    const step = NICE_STEPS.find((s) => span / s <= 140) || NICE_STEPS[NICE_STEPS.length - 1];
    const tzOff = -new Date(D.t0 + lo).getTimezoneOffset() * 60000;
    const binStart = Math.floor((D.t0 + lo + tzOff) / step) * step - tzOff - D.t0;
    const nBins = Math.max(1, Math.ceil((hi - binStart) / step));
    const xOf = (t) => PAD_L + ((t - lo) / span) * plotW;

    const keyOf = (i) => {
      const c = D.calls[i];
      if (state.stack === "model") return D.models[c[K_MODEL]];
      const a = c[K_AGENT];
      return agentInTop[a] ? `a${a}` : "Other";
    };

    const bins = [...Array(nBins)].map(() => new Map());
    const totals = new Map();
    const before = new Map(); // cumulative carry-in from calls before the window
    D.calls.forEach((c, i) => {
      if (state.focus !== null && c[K_AGENT] !== state.focus) return;
      const dt = c[K_DT];
      const parts = state.stack === "bucket" ? bucketParts(i) : { [keyOf(i)]: metricOf(i) };
      if (dt < binStart) {
        if (state.mode === "cumulative") for (const [k, v] of Object.entries(parts)) before.set(k, (before.get(k) || 0) + v);
        return;
      }
      if (dt > hi) return;
      const b = Math.min(nBins - 1, Math.floor((dt - binStart) / step));
      for (const [k, v] of Object.entries(parts)) {
        if (!(v > 0)) continue;
        bins[b].set(k, (bins[b].get(k) || 0) + v);
        totals.set(k, (totals.get(k) || 0) + v);
      }
    });

    let keys = [...totals.keys()];
    if (state.stack === "agent") {
      keys.sort((a, b) => (a === "Other") - (b === "Other") || Number(a.slice(1)) - Number(b.slice(1)));
    } else if (state.stack === "bucket") {
      const order = ["Cache Read", "Cache Write", "Uncached Input", "Text Output", "Reasoning"];
      keys.sort((a, b) => order.indexOf(a) - order.indexOf(b));
    } else {
      keys.sort((a, b) => D.models.indexOf(a) - D.models.indexOf(b));
    }
    if (state.mode === "cumulative") for (const k of before.keys()) if (!keys.includes(k)) keys.push(k);

    const colorOf = (k) => {
      if (state.stack === "agent") return k === "Other" ? PALETTE.other : agentColor[Number(k.slice(1))];
      if (state.stack === "bucket") return PALETTE.bucket[k] || PALETTE.other;
      return PALETTE.series[D.models.indexOf(k) % PALETTE.series.length];
    };
    const labelOf = (k) => {
      if (state.stack === "agent") {
        if (k === "Other") return `Other agents (${agentInTop.filter((x) => !x).length})`;
        return agentLabel(Number(k.slice(1)), 40);
      }
      return k;
    };

    // Per-bin stacked values (cumulative mode carries running totals forward)
    const running = new Map(before);
    const binVals = bins.map((m) => {
      const vals = {};
      let sum = 0;
      for (const k of keys) {
        let v = m.get(k) || 0;
        if (state.mode === "cumulative") {
          v += running.get(k) || 0;
          running.set(k, v);
        }
        vals[k] = v;
        sum += v;
      }
      return { vals, total: sum };
    });
    const maxTotal = Math.max(1e-9, ...binVals.map((b) => b.total));

    let out = "";
    for (let i = 0; i <= 4; i++) {
      const y = padT + plotH - (i / 4) * plotH;
      out += `<line x1="${PAD_L}" y1="${y.toFixed(1)}" x2="${width - PAD_R}" y2="${y.toFixed(1)}" stroke="var(--border)" stroke-dasharray="${i === 0 ? "none" : "3,3"}" />`;
      out += `<text x="${PAD_L - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="var(--text-dim)" font-size="11" font-family="var(--mono)">${fmtMetric((maxTotal * i) / 4)}</text>`;
    }
    for (const tk of timeTicks(lo, hi, plotW)) {
      const x = xOf(tk.t);
      out += `<line x1="${x.toFixed(1)}" y1="${padT + plotH}" x2="${x.toFixed(1)}" y2="${padT + plotH + 4}" stroke="var(--border-strong)" />`;
      out += `<text x="${x.toFixed(1)}" y="${height - 8}" text-anchor="middle" fill="var(--text-dim)" font-size="11" font-family="var(--mono)">${esc(tk.label)}</text>`;
    }

    const seriesIdx = Object.fromEntries(keys.map((k, i) => [k, i]));
    const binPx = (step / span) * plotW;
    const barW = Math.max(1, Math.min(34, binPx * 0.8));
    binVals.forEach((b, bi) => {
      const t = binStart + bi * step;
      const x = xOf(t) + (binPx - barW) / 2;
      if (x + barW < PAD_L || x > PAD_L + plotW) return;
      let y = padT + plotH;
      for (const k of keys) {
        const v = b.vals[k] || 0;
        if (v <= 0) continue;
        const h = Math.min(y - padT, Math.max(0.8, (v / maxTotal) * plotH));
        y -= h;
        const off = padT + plotH - (y + h);
        out += `<rect class="seg" data-s="${seriesIdx[k]}" data-off="${off.toFixed(2)}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barW.toFixed(2)}" height="${h.toFixed(2)}" fill="${colorOf(k)}" rx="${barW > 4 ? 1.2 : 0}" />`;
      }
      out += `<rect class="bar-hit" data-bin="${bi}" x="${Math.max(PAD_L, xOf(t)).toFixed(2)}" y="${padT}" width="${Math.max(1, binPx).toFixed(2)}" height="${plotH}" fill="transparent" />`;
    });
    out += attachBrush(svg, width, PAD_L, plotW, padT, plotH, (x) => lo + ((x - PAD_L) / plotW) * span);
    svg.innerHTML = out || "";

    if (!totals.size && state.mode !== "cumulative") {
      svg.insertAdjacentHTML("beforeend", `<text x="${width / 2}" y="${height / 2}" text-anchor="middle" fill="var(--text-muted)">No calls in this window.</text>`);
    }

    const subEl = document.getElementById("burn-subtitle");
    subEl.textContent = `${fmtDuration(step)} bins · Drag across the chart to zoom into a time window · Hover a legend item to highlight it${state.stack === "agent" ? ", click it to focus that agent" : ""}`;

    svg.querySelectorAll(".bar-hit").forEach((r) => {
      r.addEventListener("mousemove", (ev) => {
        if (brushing) return;
        const bi = Number(r.dataset.bin);
        const b = binVals[bi];
        const t = binStart + bi * step;
        const rows = keys
          .filter((k) => b.vals[k] > 0)
          .reverse()
          .map((k) => `<div class="tt-row"><span><span class="swatch" style="background:${colorOf(k)};margin-right:6px;"></span>${esc(labelOf(k))}</span><span class="num">${fmtMetric(b.vals[k])}</span></div>`)
          .join("");
        tooltip.innerHTML = `
          <div class="tt-title"><span>${esc(fmtClock(t, step < 60e3, !sameDay(lo, hi)))} – ${esc(fmtClock(t + step, step < 60e3))}${state.mode === "cumulative" ? " · cumulative" : ""}</span><span class="num">${fmtMetric(b.total)}</span></div>
          ${rows || '<div style="color:var(--text-dim)">No calls</div>'}`;
        positionTooltip(tooltip, ev);
      });
      r.addEventListener("mouseleave", () => {
        tooltip.hidden = true;
      });
    });

    legendEl.innerHTML = keys
      .map(
        (k) => `<span class="legend-item" data-s="${seriesIdx[k]}" ${state.stack === "agent" && k !== "Other" ? `data-agent="${k.slice(1)}"` : ""}>
          <span class="swatch" style="background:${colorOf(k)}"></span><span>${esc(labelOf(k))}</span>
          <strong class="num" style="color:var(--text)">${fmtMetric(state.mode === "cumulative" ? binVals[binVals.length - 1].vals[k] || 0 : totals.get(k) || 0)}</strong>
        </span>`
      )
      .join("");
    const segs = svg.querySelectorAll("rect.seg");
    const items = legendEl.querySelectorAll(".legend-item");
    const spot = (si) => {
      svg.classList.toggle("series-focus", si !== null);
      segs.forEach((r) => {
        const on = si !== null && r.dataset.s === si;
        r.classList.toggle("is-focus", on);
        r.style.transform = on && Number(r.dataset.off) > 0 ? `translateY(${r.dataset.off}px)` : "";
      });
      items.forEach((el) => el.classList.toggle("is-dim", si !== null && el.dataset.s !== si));
    };
    items.forEach((el) => {
      el.addEventListener("mouseenter", () => spot(el.dataset.s));
      el.addEventListener("mouseleave", () => spot(null));
      if (el.dataset.agent !== undefined) el.addEventListener("click", () => setFocus(Number(el.dataset.agent)));
    });
  }

  // ---------------------------------------------------------------------------
  // Agent lanes (Gantt) with spawn connectors
  // ---------------------------------------------------------------------------

  function renderLanes(stats) {
    const svg = document.getElementById("lanes-svg");
    const wrap = document.getElementById("lanes-wrap");
    const tooltip = document.getElementById("chart-tooltip");
    const n = D.agents.length;
    const rowH = 24, padT = 8, axisH = 24;
    const width = Math.max(360, wrap.clientWidth || 900);
    const height = padT + n * rowH + axisH;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("height", height);
    svg.style.height = height + "px";
    svg.classList.toggle("agent-focus", state.focus !== null);
    const plotW = Math.max(100, width - PAD_L - PAD_R);
    const [lo, hi] = win();
    const span = Math.max(1, hi - lo);
    const xOf = (t) => PAD_L + ((t - lo) / span) * plotW;
    const clampX = (x) => Math.max(PAD_L, Math.min(PAD_L + plotW, x));
    const rowY = (i) => padT + i * rowH;

    // Whole-session first/last per agent (lanes show lifetime, clipped to the window)
    const life = [...Array(n)].map(() => [Infinity, -Infinity]);
    for (const c of D.calls) {
      const l = life[c[K_AGENT]];
      l[0] = Math.min(l[0], c[K_DT]);
      l[1] = Math.max(l[1], c[K_DT]);
    }

    let out = "";
    for (const tk of timeTicks(lo, hi, plotW)) {
      const x = xOf(tk.t);
      out += `<line x1="${x.toFixed(1)}" y1="${padT}" x2="${x.toFixed(1)}" y2="${padT + n * rowH}" stroke="var(--border)" stroke-dasharray="2,4" />`;
      out += `<text x="${x.toFixed(1)}" y="${height - 7}" text-anchor="middle" fill="var(--text-dim)" font-size="11" font-family="var(--mono)">${esc(tk.label)}</text>`;
    }

    for (let i = 0; i < n; i++) {
      const y = rowY(i);
      const col = agentColor[i];
      const focusCls = state.focus === i ? " is-focus" : "";
      out += `<g class="lane-row" data-agent="${i}">`;
      out += `<rect class="lane-bg" x="0" y="${y}" width="${width}" height="${rowH}" fill="transparent" />`;
      out += `<text x="8" y="${y + rowH / 2 + 4}" font-size="11" fill="${i === state.focus ? "var(--text)" : "var(--text-muted)"}">${esc(agentLabel(i, 24))}</text>`;
      const [a, b] = life[i];
      if (a <= hi && b >= lo) {
        const x1 = clampX(xOf(a)), x2 = clampX(xOf(b));
        out += `<rect class="agent-mark${focusCls}" x="${x1.toFixed(1)}" y="${y + 5}" width="${Math.max(3, x2 - x1).toFixed(1)}" height="${rowH - 10}" rx="3" fill="${col}" fill-opacity="0.22" stroke="${col}" stroke-opacity="0.7" />`;
      }
      out += `</g>`;
    }
    // Call ticks (drawn after rows so connectors/ticks sit on top)
    let ticks = "";
    for (const c of D.calls) {
      if (!inWin(c[K_DT])) continue;
      const i = c[K_AGENT];
      const x = xOf(c[K_DT]);
      ticks += `<rect class="agent-mark${state.focus === i ? " is-focus" : ""}" x="${(x - 0.6).toFixed(1)}" y="${rowY(i) + 7}" width="1.3" height="${rowH - 14}" fill="${agentColor[i]}" pointer-events="none" />`;
    }
    // Spawn connectors: parent lane → child lane at the child's first call
    let links = "";
    for (let i = 1; i < n; i++) {
      const p = agentParentIdx[i];
      const t = life[i][0];
      if (p < 0 || !(t >= lo && t <= hi)) continue;
      const x = xOf(t);
      const y1 = rowY(p) + rowH / 2, y2 = rowY(i) + rowH / 2;
      const cls = `agent-mark${state.focus === i || state.focus === p ? " is-focus" : ""}`;
      links += `<line class="${cls}" x1="${x.toFixed(1)}" y1="${y1}" x2="${x.toFixed(1)}" y2="${y2}" stroke="${agentColor[i]}" stroke-dasharray="3,3" stroke-width="1.2" pointer-events="none" />`;
      links += `<circle class="${cls}" cx="${x.toFixed(1)}" cy="${y1}" r="3.2" fill="${agentColor[i]}" pointer-events="none" />`;
    }
    out += links + ticks;
    out += attachBrush(svg, width, PAD_L, plotW, padT, n * rowH, (x) => lo + ((x - PAD_L) / plotW) * span);
    svg.innerHTML = out;

    svg.querySelectorAll(".lane-row").forEach((g) => {
      const i = Number(g.dataset.agent);
      g.addEventListener("mousemove", (ev) => {
        if (brushing) return;
        const s = stats[i];
        const a = D.agents[i];
        const [f, l] = life[i];
        const parent = agentParentIdx[i] >= 0 ? agentLabel(agentParentIdx[i], 40) : "—";
        tooltip.innerHTML = `
          <div class="tt-title"><span><span class="swatch" style="background:${agentColor[i]};margin-right:6px;"></span>${esc(agentLabel(i, 44))}</span></div>
          <div class="tt-row"><span>Role</span><span>${esc(a.actor)}</span></div>
          <div class="tt-row"><span>${agentModels[i].length > 1 ? "Models" : "Model"}</span><span class="mono" style="text-align:right">${
            agentModels[i].length > 1
              ? agentModels[i].map(([m, cnt]) => `${esc(m)} (${cnt})`).join("<br/>")
              : esc((agentModels[i][0] || [a.model])[0])
          }</span></div>
          <div class="tt-row"><span>Active</span><span class="num">${esc(fmtClock(f, true))} → ${esc(fmtClock(l, true))} (${esc(fmtDuration(l - f))})</span></div>
          ${i > 0 ? `<div class="tt-row"><span>Spawned by</span><span>${esc(parent)}</span></div>` : ""}
          <div class="tt-row"><span>Calls in window</span><span class="num">${s.calls.toLocaleString()}</span></div>
          <div class="tt-row"><span>Tokens in window</span><span class="num">${fmtTok(s.total)}</span></div>
          <div class="tt-row"><span>Est. $ in window</span><span class="num">${fmtUsd(s.est)}</span></div>`;
        positionTooltip(tooltip, ev);
      });
      g.addEventListener("mouseleave", () => {
        tooltip.hidden = true;
      });
      g.addEventListener("click", () => {
        if (Date.now() - lastBrushAt < 400) return;
        setFocus(state.focus === i ? null : i);
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Context size per call
  // ---------------------------------------------------------------------------

  function renderContext() {
    const svg = document.getElementById("ctx-svg");
    const wrap = document.getElementById("ctx-wrap");
    const tooltip = document.getElementById("chart-tooltip");
    const width = Math.max(360, wrap.clientWidth || 900);
    const height = Math.max(220, wrap.clientHeight || 290);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.classList.toggle("agent-focus", state.focus !== null);
    const padT = 14, padB = 30;
    const plotW = Math.max(100, width - PAD_L - PAD_R);
    const plotH = Math.max(100, height - padT - padB);
    const [lo, hi] = win();
    const span = Math.max(1, hi - lo);
    const xOf = (t) => PAD_L + ((t - lo) / span) * plotW;

    const pts = [];
    let maxP = 1;
    D.calls.forEach((c, i) => {
      if (!inWin(c[K_DT])) return;
      const p = promptOf(c);
      maxP = Math.max(maxP, p);
      pts.push(i);
    });
    const yOf = (v) => padT + plotH - (v / maxP) * plotH;

    let out = "";
    for (let i = 0; i <= 4; i++) {
      const y = padT + plotH - (i / 4) * plotH;
      out += `<line x1="${PAD_L}" y1="${y.toFixed(1)}" x2="${width - PAD_R}" y2="${y.toFixed(1)}" stroke="var(--border)" stroke-dasharray="${i === 0 ? "none" : "3,3"}" />`;
      out += `<text x="${PAD_L - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="var(--text-dim)" font-size="11" font-family="var(--mono)">${fmtTok((maxP * i) / 4)}</text>`;
    }
    for (const tk of timeTicks(lo, hi, plotW)) {
      out += `<text x="${xOf(tk.t).toFixed(1)}" y="${height - 8}" text-anchor="middle" fill="var(--text-dim)" font-size="11" font-family="var(--mono)">${esc(tk.label)}</text>`;
    }

    // One line per agent (shows growth and compaction resets), then dots, then drop markers
    const byAgent = new Map();
    for (const i of pts) {
      const a = D.calls[i][K_AGENT];
      if (!byAgent.has(a)) byAgent.set(a, []);
      byAgent.get(a).push(i);
    }
    let lines = "", dots = "", marks = "";
    const drawDots = pts.length <= 6000;
    for (const [a, idxs] of byAgent) {
      const col = agentColor[a];
      const cls = `agent-mark${state.focus === a ? " is-focus" : ""}`;
      const d = idxs.map((i, k) => `${k ? "L" : "M"}${xOf(D.calls[i][K_DT]).toFixed(1)},${yOf(promptOf(D.calls[i])).toFixed(1)}`).join("");
      lines += `<path class="${cls}" d="${d}" fill="none" stroke="${col}" stroke-width="${a === 0 ? 1.8 : 1.2}" stroke-opacity="0.75" />`;
      let prev = 0;
      for (const i of idxs) {
        const c = D.calls[i];
        const p = promptOf(c);
        const x = xOf(c[K_DT]).toFixed(1), y = yOf(p).toFixed(1);
        if (drawDots) dots += `<circle class="${cls}" cx="${x}" cy="${y}" r="2.3" fill="${col}" />`;
        if (prev > 20000 && p < prev * 0.6) {
          marks += `<text class="${cls}" x="${x}" y="${(yOf(prev) - 4).toFixed(1)}" text-anchor="middle" font-size="10" fill="${col}">▼</text>`;
        }
        prev = p;
      }
    }
    const crossY = `<line class="ctx-cross" x1="0" y1="${padT}" x2="0" y2="${padT + plotH}" stroke="var(--text-dim)" stroke-dasharray="3,3" visibility="hidden" />`;
    out += lines + dots + marks + crossY;
    out += attachBrush(svg, width, PAD_L, plotW, padT, plotH, (x) => lo + ((x - PAD_L) / plotW) * span);
    out += `<rect class="ctx-hit" x="${PAD_L}" y="${padT}" width="${plotW}" height="${plotH}" fill="transparent" />`;
    if (!pts.length) out += `<text x="${width / 2}" y="${height / 2}" text-anchor="middle" fill="var(--text-muted)">No calls in this window.</text>`;
    svg.innerHTML = out;

    // Hover: nearest call to the pointer (screen distance), respecting agent focus
    const hit = svg.querySelector(".ctx-hit");
    const cross = svg.querySelector(".ctx-cross");
    hit.addEventListener("mousemove", (ev) => {
      if (brushing) return;
      const box = svg.getBoundingClientRect();
      const sx = width / (box.width || 1);
      const mx = (ev.clientX - box.left) * sx, my = (ev.clientY - box.top) * sx;
      let best = -1, bestD = Infinity;
      for (const i of pts) {
        const c = D.calls[i];
        if (state.focus !== null && c[K_AGENT] !== state.focus) continue;
        const dx = xOf(c[K_DT]) - mx, dy = (yOf(promptOf(c)) - my) * 0.35;
        const dd = dx * dx + dy * dy;
        if (dd < bestD) {
          bestD = dd;
          best = i;
        }
      }
      if (best < 0 || bestD > 40 * 40) {
        tooltip.hidden = true;
        cross.setAttribute("visibility", "hidden");
        return;
      }
      const c = D.calls[best];
      const x = xOf(c[K_DT]);
      cross.setAttribute("x1", x.toFixed(1));
      cross.setAttribute("x2", x.toFixed(1));
      cross.setAttribute("visibility", "visible");
      tooltip.innerHTML = `
        <div class="tt-title"><span><span class="swatch" style="background:${agentColor[c[K_AGENT]]};margin-right:6px;"></span>${esc(agentLabel(c[K_AGENT], 40))}</span></div>
        <div class="tt-row"><span>${esc(fmtDateTime(c[K_DT]))}</span><span class="mono">${esc(D.models[c[K_MODEL]])}</span></div>
        <div class="tt-row"><span>Prompt (context)</span><span class="num"><strong>${fmtTok(promptOf(c))}</strong></span></div>
        <div class="tt-row"><span>· cache read / write</span><span class="num">${fmtTok(c[K_CR])} / ${fmtTok(c[K_CW])}</span></div>
        <div class="tt-row"><span>· uncached input</span><span class="num">${fmtTok(c[K_IN])}</span></div>
        <div class="tt-row"><span>Output / reasoning</span><span class="num">${fmtTok(c[K_OUT])} / ${fmtTok(c[K_REAS])}</span></div>
        <div class="tt-row"><span>Est. $</span><span class="num">${fmtUsd(callEst[best])}</span></div>`;
      positionTooltip(tooltip, ev);
    });
    hit.addEventListener("mouseleave", () => {
      tooltip.hidden = true;
      cross.setAttribute("visibility", "hidden");
    });
  }

  // ---------------------------------------------------------------------------
  // Agents table
  // ---------------------------------------------------------------------------

  function renderTable(stats) {
    const wrap = document.getElementById("agents-table-wrap");
    const rows = stats
      .map((s) => {
        const a = D.agents[s.idx];
        const sel = state.focus === s.idx ? "selected-row" : "";
        const aCol = PALETTE.actor[a.actor] || "#94a3b8";
        const has = s.calls > 0;
        return `
          <tr class="clickable ${sel}" data-agent="${s.idx}" style="${has ? "" : "opacity:0.5"}">
            <td><div class="agent-cell"><span class="dot" style="background:${agentColor[s.idx]}"></span><span title="${esc(a.title)}">${esc(agentLabel(s.idx, 70))}</span></div></td>
            <td><span class="badge"><span class="dot" style="background:${aCol}"></span>${esc(a.actor)}</span></td>
            <td class="mono" style="font-size:11.5px;white-space:nowrap" title="${esc([...s.models].join(", "))}">${esc([...s.models][0] || a.model)}${s.models.size > 1 ? ` <span style="color:var(--text-dim)">+${s.models.size - 1}</span>` : ""}</td>
            <td class="num" style="white-space:nowrap">${has ? esc(fmtClock(s.first, true)) : "—"}</td>
            <td class="num" style="white-space:nowrap">${has ? esc(fmtDuration(s.last - s.first)) : "—"}</td>
            <td class="right num">${s.calls.toLocaleString()}</td>
            <td class="right num">${fmtTok(s.active)}</td>
            <td class="right num">${fmtTok(s.cr)}</td>
            <td class="right num">${fmtTok(s.generated)}</td>
            <td class="right num"><strong>${fmtTok(s.total)}</strong></td>
            <td class="right num">${fmtUsd(s.est)}</td>
            <td class="right num">${fmtTok(s.peak)}</td>
            <td class="right num">${s.compactions || "—"}</td>
          </tr>`;
      })
      .join("");
    wrap.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th>Agent</th><th>Role</th><th>Model</th><th>Start</th><th>Span</th>
          <th class="right">Calls</th><th class="right">Fresh/Active</th><th class="right">Cache Read</th>
          <th class="right">Out+Reason</th><th class="right">Total</th><th class="right">Est. $</th>
          <th class="right">Peak Ctx</th><th class="right">Drops</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    wrap.querySelectorAll("tr[data-agent]").forEach((tr) => {
      tr.addEventListener("click", () => {
        const i = Number(tr.dataset.agent);
        setFocus(state.focus === i ? null : i);
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Orchestration
  // ---------------------------------------------------------------------------

  function setFocus(i) {
    state.focus = i;
    renderAll();
  }

  function renderAll() {
    const stats = agentStats();
    renderWindowControls();
    renderKPIs(stats);
    renderBurn();
    renderLanes(stats);
    renderContext();
    renderTable(stats);
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    PALETTE = theme === "light" ? T.PALETTES.light : T.PALETTES.dark;
  }

  function init() {
    prepare();
    if (focusParam) {
      const idx = D.agents.findIndex((a) => a.id === focusParam);
      if (idx > 0) state.focus = idx;
    }
    if (D.agents.length <= 1) {
      state.stack = "bucket";
      document.querySelectorAll("#stack-group .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.stack === "bucket"));
    }
    document.getElementById("sess-body").hidden = false;
    renderHeader();
    renderAll();
  }

  function boot() {
    applyTheme(T.savedTheme());
    document.getElementById("btn-theme").addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
      T.saveTheme(next);
      applyTheme(next);
      if (D) {
        prepare();
        renderHeader();
        renderAll();
      }
    });
    document.getElementById("metric-select").addEventListener("change", (e) => {
      state.metric = e.target.value;
      renderAll();
    });
    document.querySelectorAll("#stack-group .seg-btn").forEach((btn) =>
      btn.addEventListener("click", () => {
        state.stack = btn.dataset.stack;
        document.querySelectorAll("#stack-group .seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
        renderAll();
      })
    );
    document.querySelectorAll("#mode-group .seg-btn").forEach((btn) =>
      btn.addEventListener("click", () => {
        state.mode = btn.dataset.mode;
        document.querySelectorAll("#mode-group .seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
        renderAll();
      })
    );
    document.getElementById("btn-reset-zoom").addEventListener("click", () => {
      state.win = null;
      renderAll();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && (state.win || state.focus !== null)) {
        if (state.win) state.win = null;
        else state.focus = null;
        renderAll();
      }
    });
    let raf = 0;
    window.addEventListener("resize", () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => D && renderAll());
    });

    if (!/^[0-9a-f]{16}$/.test(detailId)) {
      fail("No session selected. Open a session from the Heavyweight Sessions table on the observatory page.");
      return;
    }
    const sc = document.createElement("script");
    sc.src = `./sessions/${detailId}.js`;
    sc.onload = () => {
      D = window.__TOKEN_SESSION__ || null;
      if (!D || !Array.isArray(D.calls) || !D.calls.length) {
        fail("This session has no per-call timeline data.");
        return;
      }
      init();
    };
    sc.onerror = () => fail("Timeline file not found. Re-run `token-stats` (or `token-stats extract`) to regenerate session timelines.");
    document.body.appendChild(sc);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
