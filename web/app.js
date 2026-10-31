(() => {
  "use strict";

  // Theme-aware palettes. Harness / provider hues follow vendor branding (Claude = clay orange,
  // OpenAI / Codex = blue, Google / Antigravity = green) and were checked for normal-vision and
  // colour-blind (protan/deutan) separation against each theme's card surface.
  // `series` is the categorical set for open-ended dimensions (projects, models): 10 hues that are
  // mutually distinct across ALL pairs (not only neighbours), assigned per entity so no two series
  // visible in the same chart ever share a colour. "Other" is always the neutral slate.
  const PALETTES = {
    dark: {
      harness: {
        "Claude Code": "#e07a55",
        "Codex": "#5a95f5",
        "OpenCode": "#c94fa8",
        "Antigravity": "#86e3a5",
      },
      provider: {
        "Anthropic": "#e07a55",
        "OpenAI": "#5a95f5",
        "Google": "#86e3a5",
        "Local / Self-Hosted GPU": "#c94fa8",
        "Other": "#94a3b8",
      },
      actor: {
        "subagent": "#a78bfa",
        "main": "#38bdf8",
        "auto_review": "#fbbf24",
      },
      bucket: {
        "Cache Read": "#475569",
        "Cache Write": "#f59e0b",
        "Uncached Input": "#38bdf8",
        "Text Output": "#34d399",
        "Reasoning": "#f472b6",
        "Subscriptions": "#38bdf8",
        "Overages / Credits": "#f59e0b",
      },
      series: [
        "#60d0fe", "#fb4c10", "#92e62c", "#9563fe", "#c5ad4c",
        "#d57ba0", "#36d4a6", "#759ae6", "#c33dbd", "#009c1e",
      ],
      other: "#64748b",
      // GitHub-style activity ramp: empty cell + 4 intensity steps (one hue, dim -> bright)
      heat: ["#1a2131", "#173f73", "#1f62b3", "#3987e5", "#9ec5f4"],
    },
    light: {
      harness: {
        "Claude Code": "#e0784a",
        "Codex": "#2563eb",
        "OpenCode": "#b8389a",
        "Antigravity": "#0f6b2f",
      },
      provider: {
        "Anthropic": "#e0784a",
        "OpenAI": "#2563eb",
        "Google": "#0f6b2f",
        "Local / Self-Hosted GPU": "#b8389a",
        "Other": "#64748b",
      },
      actor: {
        "subagent": "#7c3aed",
        "main": "#0284c7",
        "auto_review": "#c98500",
      },
      bucket: {
        "Cache Read": "#94a3b8",
        "Cache Write": "#d97706",
        "Uncached Input": "#0284c7",
        "Text Output": "#059669",
        "Reasoning": "#db2777",
        "Subscriptions": "#0284c7",
        "Overages / Credits": "#d97706",
      },
      series: [
        "#0d76ad", "#cd7d09", "#5ccc22", "#9754ed", "#b24358",
        "#026812", "#ef74fb", "#3f34cb", "#fb5b7b", "#851690",
      ],
      other: "#94a3b8",
      heat: ["#eef2f8", "#b7d3f6", "#6da7ec", "#2a78d6", "#104281"],
    },
  };
  let PALETTE = PALETTES.dark;

  function applyThemePalette(theme) {
    PALETTE = theme === "light" ? PALETTES.light : PALETTES.dark;
  }

  // Number of named series in stacked charts before the remainder folds into "Other"
  const TOP_SERIES = 10;

  const ACTOR_LABELS = {
    subagent: "Subagent Swarm",
    main: "Main Interactive",
    auto_review: "Auto-Review / Guardian",
  };

  const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // Cube column indices matching token_stats.py
  const C_DATE = 0;
  const C_HARNESS = 1;
  const C_PROVIDER = 2;
  const C_MODEL = 3;
  const C_PROJECT = 4;
  const C_ACTOR = 5;
  const C_ARCHIVED = 6;
  const C_CALLS = 7;
  const C_SESSIONS = 8;
  const C_IN = 9;
  const C_CR = 10;
  const C_CW = 11;
  const C_OUT = 12;
  const C_REAS = 13;
  const C_COST = 14;
  const C_HOUR = 15; // only present on hourly rows built from rawData.cube_hourly

  const STORAGE_RATES_KEY = "token_obs_rates_v1";
  const STORAGE_BILLING_KEY = "token_obs_billing_v1";
  const STORAGE_THEME_KEY = "token_obs_theme_v1";

  let rawData = window.__TOKEN_STATS__ || null;
  let customRates = loadSavedRates();
  let billingOverrides = loadSavedBillingOverrides();
  let rowBillingMap = new WeakMap();

  const state = {
    dateFrom: "",
    dateTo: "",
    rangePreset: "all",
    metric: "total", // total | active | generated | est_cost
    includeArchived: true,
    showPaths: false,
    chartStack: "harness", // harness | provider | project | model | bucket | actor
    chartGrain: "day", // hour | day | week
    matrixMode: "project_harness",
    modelSearch: "",
    sessionSearch: "",
    filters: {
      harness: null,
      provider: null,
      model: null,
      project: null,
      actor: null,
    },
    sorts: {
      harness: { col: "primary", dir: -1 },
      provider: { col: "primary", dir: -1 },
      model: { col: "primary", dir: -1 },
      project: { col: "primary", dir: -1 },
      sessions: { col: "total", dir: -1 },
    },
  };

  // -------------------------------------------------------------------------
  // Formatting & Math Helpers
  // -------------------------------------------------------------------------

  function fmtTok(n) {
    const abs = Math.abs(n || 0);
    if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (abs >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (abs >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return Math.round(n || 0).toLocaleString();
  }

  function fmtUsd(n) {
    const v = Number(n || 0);
    if (v >= 1000) return "$" + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
    if (v >= 10) return "$" + v.toFixed(2);
    if (v > 0 && v < 0.01) return "<$0.01";
    return "$" + v.toFixed(2);
  }

  function fmtMetric(val, metric = state.metric) {
    return metric === "est_cost" || metric === "paid_cost" ? fmtUsd(val) : fmtTok(val);
  }

  function metricColLabel(short = false) {
    if (state.metric === "est_cost") return short ? "Est. $" : "Est. API $";
    if (state.metric === "paid_cost") return short ? "Paid $" : "Paid $";
    return "Selected";
  }

  function fmtPct(num, den) {
    if (!den || den <= 0) return "0.0%";
    return ((num / den) * 100).toFixed(1) + "%";
  }

  function esc(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Stable, collision-free colours for open-ended dimensions (project, model, ...). Each entity keeps
  // the palette slot it was first given; if two entities visible in the same chart would share a
  // slot, the newcomer moves to a free slot (and keeps it), so colours follow the entity, not rank.
  const seriesSlotRegistry = new Map(); // dim -> Map(key -> slot index)

  function seedSeriesSlots() {
    seriesSlotRegistry.clear();
    if (!rawData || !rawData.cube) return;
    for (const [dim, col] of [["project", C_PROJECT], ["model", C_MODEL], ["harness", C_HARNESS], ["provider", C_PROVIDER], ["actor", C_ACTOR]]) {
      const totals = new Map();
      for (const r of rawData.cube) {
        const k = r[col];
        totals.set(k, (totals.get(k) || 0) + r[C_IN] + r[C_CR] + r[C_CW] + r[C_OUT] + r[C_REAS]);
      }
      const reg = new Map();
      [...totals.entries()]
        .sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])))
        .forEach(([k], i) => reg.set(k, i % PALETTE.series.length));
      seriesSlotRegistry.set(dim, reg);
    }
  }

  function seriesColorMap(dim, keys) {
    const fixed = PALETTE[dim] && dim !== "series" ? PALETTE[dim] : null;
    const out = {};
    const used = new Set();
    // Pass 1: fixed brand colours, plus registry slots that are still free in this view
    if (!seriesSlotRegistry.has(dim)) seriesSlotRegistry.set(dim, new Map());
    const reg = seriesSlotRegistry.get(dim);
    const pending = [];
    for (const k of keys) {
      if (k === "Other") {
        out[k] = PALETTE.other;
        continue;
      }
      if (fixed && fixed[k]) {
        out[k] = fixed[k];
        used.add(fixed[k]);
        continue;
      }
      pending.push(k);
    }
    const unresolved = [];
    for (const k of pending) {
      const slot = reg.get(k);
      const c = slot !== undefined ? PALETTE.series[slot] : null;
      if (c && !used.has(c)) {
        out[k] = c;
        used.add(c);
      } else {
        unresolved.push(k);
      }
    }
    // Pass 2: give clashing / new entities the first free slot and remember it
    for (const k of unresolved) {
      let slot = PALETTE.series.findIndex((c) => !used.has(c));
      if (slot < 0) slot = (reg.get(k) || 0) % PALETTE.series.length;
      reg.set(k, slot);
      out[k] = PALETTE.series[slot];
      used.add(out[k]);
    }
    return out;
  }

  function loadSavedRates() {
    try {
      const raw = localStorage.getItem(STORAGE_RATES_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch {
      return {};
    }
  }

  function loadSavedBillingOverrides() {
    const serverOv = (rawData && rawData.billing && rawData.billing.saved_overrides) || null;
    if (serverOv && (Object.keys(serverOv.items || {}).length || (serverOv.custom_items || []).length)) {
      return {
        items: { ...(serverOv.items || {}) },
        custom_items: [...(serverOv.custom_items || [])],
      };
    }
    try {
      const raw = localStorage.getItem(STORAGE_BILLING_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        return {
          items: { ...(parsed.items || {}) },
          custom_items: [...(parsed.custom_items || [])],
        };
      }
    } catch {
      // ignore
    }
    return { items: {}, custom_items: [] };
  }

  function getEffectiveLedger() {
    const bill = (rawData && rawData.billing) || {};
    const defs = bill.default_ledger || [];
    const ovItems = (billingOverrides && billingOverrides.items) || {};
    const out = defs.map((d) => {
      const ov = ovItems[d.id] || {};
      return {
        ...d,
        enabled: ov.enabled !== false,
        label: ov.label !== undefined ? ov.label : d.label,
        start_date: ov.start_date || d.start_date,
        end_date: ov.end_date || d.end_date,
        amount_usd: ov.amount_usd !== undefined ? Number(ov.amount_usd) : Number(d.amount_usd || 0),
        is_custom: false,
      };
    });
    const todayIso = new Date().toISOString().slice(0, 10);
    for (const c of (billingOverrides && billingOverrides.custom_items) || []) {
      out.push({
        id: c.id || "custom_" + Math.random().toString(36).slice(2, 8),
        harness: c.harness || "Claude Code",
        category: c.category || "overage",
        label: c.label || "Custom entry",
        start_date: c.start_date || todayIso,
        end_date: c.end_date || todayIso,
        amount_usd: Number(c.amount_usd || 0),
        promo_value_usd: 0,
        consumed_usd: Number(c.amount_usd || 0),
        remaining_usd: 0,
        source: "custom",
        detail: c.detail || "Custom user-added billing entry",
        enabled: c.enabled !== false,
        is_custom: true,
      });
    }
    return out;
  }

  function getRowBilling(row) {
    return rowBillingMap.get(row) || { paid: 0, sub: 0, ov: 0, promo: 0 };
  }

  function recomputeBillingAllocations() {
    rowBillingMap = new WeakMap();
    if (!rawData || !rawData.cube) return;
    const cube = rawData.cube;
    const n = cube.length;
    const estArr = new Float64Array(n);
    const subArr = new Float64Array(n);
    const ovArr = new Float64Array(n);
    const promoArr = new Float64Array(n);
    const bill = rawData.billing || {};
    const promoProvs = new Set((bill.opencode_telemetry && bill.opencode_telemetry.promo_providers) || []);

    for (let i = 0; i < n; i++) {
      const r = cube[i];
      if (!state.includeArchived && r[C_ARCHIVED]) continue;
      estArr[i] = calcEstCost(r[C_MODEL], r[C_IN], r[C_CR], r[C_CW], r[C_OUT], r[C_REAS]);
      if (r[C_HARNESS] === "OpenCode" && promoProvs.has(r[C_PROVIDER]) && r[C_COST] > 0) {
        promoArr[i] = r[C_COST];
      }
    }

    const codexCells = (bill.codex_wallet && bill.codex_wallet.burns_by_cell_usd) || {};
    const claudeOvDays = (bill.claude_telemetry && bill.claude_telemetry.overage_day_weights) || {};

    // Pre-sum estCost per Codex (date|proj|model) cell so multiple actors split cell burn cleanly
    const codexCellEstSum = new Map();
    for (let i = 0; i < n; i++) {
      const r = cube[i];
      if (r[C_HARNESS] !== "Codex") continue;
      if (!state.includeArchived && r[C_ARCHIVED]) continue;
      const ck = `${r[C_DATE]}|${r[C_PROJECT]}|${r[C_MODEL]}`;
      codexCellEstSum.set(ck, (codexCellEstSum.get(ck) || 0) + Math.max(1e-9, estArr[i]));
    }

    const ledger = getEffectiveLedger();
    for (const it of ledger) {
      if (!it.enabled) continue;
      const amt = Number(it.amount_usd || 0);
      if (amt <= 0) continue;

      let candidates = [];
      for (let i = 0; i < n; i++) {
        const r = cube[i];
        if (!state.includeArchived && r[C_ARCHIVED]) continue;
        if (r[C_HARNESS] !== it.harness) continue;
        const d = r[C_DATE];
        if (d !== "unknown" && it.start_date && d < it.start_date) continue;
        if (d !== "unknown" && it.end_date && d > it.end_date) continue;
        candidates.push(i);
      }
      if (!candidates.length) {
        for (let i = 0; i < n; i++) {
          const r = cube[i];
          if (!state.includeArchived && r[C_ARCHIVED]) continue;
          if (r[C_HARNESS] === it.harness) candidates.push(i);
        }
      }
      if (!candidates.length) continue;

      const weights = new Float64Array(candidates.length);
      let wSum = 0;

      if (it.id === "codex_credits" && Object.keys(codexCells).length > 0) {
        for (let j = 0; j < candidates.length; j++) {
          const idx = candidates[j];
          const r = cube[idx];
          const ck = `${r[C_DATE]}|${r[C_PROJECT]}|${r[C_MODEL]}`;
          const cellBurn = codexCells[ck] || 0;
          if (cellBurn > 0) {
            const denom = codexCellEstSum.get(ck) || 1;
            weights[j] = cellBurn * (Math.max(1e-9, estArr[idx]) / denom);
            wSum += weights[j];
          }
        }
      } else if (it.id === "claude_overage" && Object.keys(claudeOvDays).length > 0) {
        for (let j = 0; j < candidates.length; j++) {
          const idx = candidates[j];
          const r = cube[idx];
          const dw = claudeOvDays[r[C_DATE]] || 0;
          const isFable = String(r[C_MODEL]).toLowerCase().includes("fable");
          if (dw > 0 || isFable) {
            weights[j] = Math.max(1e-6, estArr[idx]) * (dw > 0 ? dw : 25) * (isFable ? 1.8 : 1.0);
            wSum += weights[j];
          }
        }
      }

      if (wSum <= 0) {
        for (let j = 0; j < candidates.length; j++) {
          const idx = candidates[j];
          const r = cube[idx];
          const tok = r[C_IN] + r[C_CR] + r[C_CW] + r[C_OUT] + r[C_REAS];
          weights[j] = estArr[idx] > 0 ? estArr[idx] : Math.max(1, tok);
          wSum += weights[j];
        }
      }

      if (wSum > 0) {
        const isSub = it.category === "subscription";
        for (let j = 0; j < candidates.length; j++) {
          const idx = candidates[j];
          const shareUsd = amt * (weights[j] / wSum);
          if (isSub) subArr[idx] += shareUsd;
          else ovArr[idx] += shareUsd;
        }
      }
    }

    for (let i = 0; i < n; i++) {
      const sub = subArr[i];
      const ov = ovArr[i];
      const promo = promoArr[i];
      if (sub > 0 || ov > 0 || promo > 0) {
        rowBillingMap.set(cube[i], { paid: sub + ov, sub, ov, promo });
      }
    }
    allocateHourlyBilling();
  }

  // Hourly rows: full-width cube rows (dims copied from the parent daily row) carrying one local hour
  // of that cell's usage, plus the hour itself at C_HOUR. Paid-$ is apportioned from the parent row.
  let hourlyRows = [];

  function buildHourlyRows() {
    hourlyRows = [];
    const hc = rawData && rawData.cube_hourly;
    if (!hc || !rawData.cube) return;
    for (const h of hc) {
      const parent = rawData.cube[h[0]];
      if (!parent) continue;
      const row = [
        parent[C_DATE], parent[C_HARNESS], parent[C_PROVIDER], parent[C_MODEL], parent[C_PROJECT],
        parent[C_ACTOR], parent[C_ARCHIVED],
        h[2], 0, h[3], h[4], h[5], h[6], h[7], h[8],
        h[1],
      ];
      row.parent = parent;
      hourlyRows.push(row);
    }
  }

  function onDataLoaded() {
    buildHourlyRows();
    seedSeriesSlots();
  }

  function allocateHourlyBilling() {
    for (const row of hourlyRows) {
      const pb = rowBillingMap.get(row.parent);
      if (!pb) continue;
      const p = row.parent;
      const estP = calcEstCost(p[C_MODEL], p[C_IN], p[C_CR], p[C_CW], p[C_OUT], p[C_REAS]);
      const estH = calcEstCost(row[C_MODEL], row[C_IN], row[C_CR], row[C_CW], row[C_OUT], row[C_REAS]);
      const tokP = p[C_IN] + p[C_CR] + p[C_CW] + p[C_OUT] + p[C_REAS];
      const tokH = row[C_IN] + row[C_CR] + row[C_CW] + row[C_OUT] + row[C_REAS];
      const share = estP > 0 ? estH / estP : tokP > 0 ? tokH / tokP : 0;
      const promoShare = p[C_COST] > 0 ? row[C_COST] / p[C_COST] : share;
      rowBillingMap.set(row, {
        paid: pb.paid * share,
        sub: pb.sub * share,
        ov: pb.ov * share,
        promo: pb.promo * promoShare,
      });
    }
  }

  function calcLedgerOverlapFraction(startDate, endDate) {
    if (state.rangePreset === "all" || (!state.dateFrom && !state.dateTo)) return 1.0;
    const s = startDate || "2026-01-01";
    const e = endDate || s;
    const wFrom = state.dateFrom || "0000-01-01";
    const wTo = state.dateTo || "9999-12-31";
    const effS = s > wFrom ? s : wFrom;
    const effE = e < wTo ? e : wTo;
    if (effS > effE) return 0.0;
    const totDays = Math.max(1, Math.round((Date.parse(e) - Date.parse(s)) / 86400000) + 1);
    const ovDays = Math.max(0, Math.round((Date.parse(effE) - Date.parse(effS)) / 86400000) + 1);
    return Math.min(1.0, ovDays / totDays);
  }

  function getDefaultModelRate(model) {
    const defs = (rawData && rawData.default_rates_per_1m) || {};
    return defs[model] || { in: 2.0, cr: 0.2, cw: 2.5, out: 10.0, note: "Default" };
  }

  function getModelRate(model) {
    if (customRates && customRates[model]) return customRates[model];
    return getDefaultModelRate(model);
  }

  function calcEstCost(model, inp, cr, cw, outTxt, reas) {
    const r = getModelRate(model);
    return (
      (inp * (r.in || 0) +
        cr * (r.cr || 0) +
        cw * (r.cw || 0) +
        (outTxt + reas) * (r.out || 0)) /
      1e6
    );
  }

  function calcUncachedCounterfactualCost(model, inp, cr, cw, outTxt, reas) {
    const r = getModelRate(model);
    return (
      ((inp + cr + cw) * (r.in || 0) + (outTxt + reas) * (r.out || 0)) / 1e6
    );
  }

  function emptyAgg(name = "") {
    return {
      name,
      calls: 0,
      sessions: 0,
      inp: 0,
      cr: 0,
      cw: 0,
      out: 0,
      reas: 0,
      loggedCost: 0,
      paidCost: 0,
      subCost: 0,
      overageCost: 0,
      promoCost: 0,
      estCost: 0,
      noCacheCost: 0,
      harnesses: new Set(),
      providers: new Set(),
      models: new Set(),
    };
  }

  function addRowToAgg(agg, row) {
    const model = row[C_MODEL];
    const inp = row[C_IN];
    const cr = row[C_CR];
    const cw = row[C_CW];
    const out = row[C_OUT];
    const reas = row[C_REAS];
    const rb = getRowBilling(row);
    agg.calls += row[C_CALLS];
    agg.sessions += row[C_SESSIONS];
    agg.inp += inp;
    agg.cr += cr;
    agg.cw += cw;
    agg.out += out;
    agg.reas += reas;
    agg.loggedCost += row[C_COST];
    agg.paidCost += rb.paid;
    agg.subCost += rb.sub;
    agg.overageCost += rb.ov;
    agg.promoCost += rb.promo;
    agg.estCost += calcEstCost(model, inp, cr, cw, out, reas);
    agg.noCacheCost += calcUncachedCounterfactualCost(model, inp, cr, cw, out, reas);
    agg.harnesses.add(row[C_HARNESS]);
    agg.providers.add(row[C_PROVIDER]);
    agg.models.add(model);
  }

  function finalizeAgg(agg) {
    agg.total = agg.inp + agg.cr + agg.cw + agg.out + agg.reas;
    agg.active = agg.inp + agg.cw + agg.out + agg.reas;
    agg.generated = agg.out + agg.reas;
    agg.totalIn = agg.inp + agg.cr + agg.cw;
    agg.cacheHit = agg.totalIn > 0 ? agg.cr / agg.totalIn : 0;
    agg.primary = metricFromAgg(agg, state.metric);
    return agg;
  }

  function metricFromAgg(agg, metric = state.metric) {
    if (metric === "active") return agg.inp + agg.cw + agg.out + agg.reas;
    if (metric === "generated") return agg.out + agg.reas;
    if (metric === "paid_cost") return agg.paidCost;
    if (metric === "est_cost") return agg.estCost;
    return agg.inp + agg.cr + agg.cw + agg.out + agg.reas;
  }

  function metricFromRow(row, metric = state.metric) {
    const inp = row[C_IN], cr = row[C_CR], cw = row[C_CW], out = row[C_OUT], reas = row[C_REAS];
    if (metric === "active") return inp + cw + out + reas;
    if (metric === "generated") return out + reas;
    if (metric === "paid_cost") return getRowBilling(row).paid;
    if (metric === "est_cost") return calcEstCost(row[C_MODEL], inp, cr, cw, out, reas);
    return inp + cr + cw + out + reas;
  }

  function weekStartISO(dateStr) {
    if (!dateStr || dateStr === "unknown") return "unknown";
    const [y, m, d] = dateStr.split("-").map(Number);
    const dtObj = new Date(Date.UTC(y, m - 1, d));
    const day = dtObj.getUTCDay(); // 0=Sun..6=Sat
    const diff = day === 0 ? -6 : 1 - day; // Monday start
    dtObj.setUTCDate(dtObj.getUTCDate() + diff);
    return dtObj.toISOString().slice(0, 10);
  }

  function getMinMaxDates(cube) {
    let minD = "9999-99-99";
    let maxD = "0000-00-00";
    for (const r of cube) {
      const d = r[C_DATE];
      if (!d || d === "unknown") continue;
      if (!state.includeArchived && r[C_ARCHIVED]) continue;
      if (d < minD) minD = d;
      if (d > maxD) maxD = d;
    }
    if (minD > maxD) {
      const today = new Date().toISOString().slice(0, 10);
      return [today, today];
    }
    return [minD, maxD];
  }

  function renderMonthPresets() {
    const holder = document.getElementById("month-presets");
    if (!holder || !rawData || !rawData.cube) return;
    const months = new Set();
    for (const r of rawData.cube) {
      const d = r[C_DATE];
      if (d && d !== "unknown" && d.length >= 7) {
        if (!state.includeArchived && r[C_ARCHIVED]) continue;
        months.add(d.slice(0, 7));
      }
    }
    const sorted = [...months].sort().reverse().slice(0, 5);
    const multiYear = new Set(sorted.map((ym) => ym.slice(0, 4))).size > 1;
    holder.innerHTML = sorted
      .map((ym) => {
        const [y, m] = ym.split("-").map(Number);
        const lbl = multiYear ? `${MONTH_NAMES[m - 1]} '${String(y).slice(2)}` : MONTH_NAMES[m - 1];
        const act = state.rangePreset === ym ? "active" : "";
        return `<button class="seg-btn ${act}" data-range="${esc(ym)}">${esc(lbl)}</button>`;
      })
      .join("");
    holder.querySelectorAll(".seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        applyRangePreset(btn.dataset.range);
        renderAll();
      });
    });
  }

  // -------------------------------------------------------------------------
  // Filtering Engine
  // -------------------------------------------------------------------------

  function dateMatches(d) {
    if (!d || d === "unknown") {
      return state.rangePreset === "all";
    }
    if (state.dateFrom && d < state.dateFrom) return false;
    if (state.dateTo && d > state.dateTo) return false;
    return true;
  }

  function rowMatches(r, ignoreDim = null) {
    if (!state.includeArchived && r[C_ARCHIVED]) return false;
    if (!dateMatches(r[C_DATE])) return false;
    const f = state.filters;
    if (ignoreDim !== "harness" && f.harness && r[C_HARNESS] !== f.harness) return false;
    if (ignoreDim !== "provider" && f.provider && r[C_PROVIDER] !== f.provider) return false;
    if (ignoreDim !== "model" && f.model && r[C_MODEL] !== f.model) return false;
    if (ignoreDim !== "project" && f.project && r[C_PROJECT] !== f.project) return false;
    if (ignoreDim !== "actor" && f.actor && r[C_ACTOR] !== f.actor) return false;
    return true;
  }

  function sessionMatches(s) {
    if (!dateMatches(s.date)) return false;
    const f = state.filters;
    if (f.harness && s.harness !== f.harness) return false;
    if (f.provider && s.provider !== f.provider) return false;
    if (f.model && s.model !== f.model) return false;
    if (f.project && s.project !== f.project) return false;
    if (f.actor && s.actor !== f.actor) return false;
    return true;
  }

  function toggleFilter(dim, value) {
    state.filters[dim] = state.filters[dim] === value ? null : value;
    renderAll();
  }

  function clearAllFilters() {
    for (const k of Object.keys(state.filters)) state.filters[k] = null;
    applyRangePreset("all");
    renderAll();
  }

  function applyRangePreset(preset) {
    state.rangePreset = preset;
    const [minD, maxD] = getMinMaxDates(rawData.cube);
    if (preset === "all") {
      state.dateFrom = minD;
      state.dateTo = maxD;
    } else if (preset.endsWith("d")) {
      const days = parseInt(preset, 10);
      const [y, m, d] = maxD.split("-").map(Number);
      const end = new Date(Date.UTC(y, m - 1, d));
      end.setUTCDate(end.getUTCDate() - (days - 1));
      state.dateFrom = end.toISOString().slice(0, 10);
      state.dateTo = maxD;
    } else if (/^\d{4}-\d{2}$/.test(preset)) {
      const [y, m] = preset.split("-").map(Number);
      const lastDay = new Date(Date.UTC(y, m, 0)).getUTCDate();
      state.dateFrom = `${preset}-01`;
      state.dateTo = `${preset}-${String(lastDay).padStart(2, "0")}`;
    }
    document.getElementById("date-from").value = state.dateFrom;
    document.getElementById("date-to").value = state.dateTo;
    document.querySelectorAll("#range-presets .seg-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.range === preset);
    });
  }

  // -------------------------------------------------------------------------
  // Rendering Sections
  // -------------------------------------------------------------------------

  function renderHeaderAndChips(matchedSessionCount) {
    const sub = document.getElementById("meta-subtitle");
    const genDate = rawData.generated_at ? rawData.generated_at.replace("T", " ") : "";
    const anyFilter =
      state.rangePreset !== "all" || Object.values(state.filters).some((v) => Boolean(v));
    const sessNote = anyFilter
      ? `${matchedSessionCount.toLocaleString()} of ${rawData.total_sessions_indexed.toLocaleString()} sessions matching filters`
      : `${rawData.total_sessions_indexed.toLocaleString()} sessions indexed across 4 harnesses`;
    sub.textContent = `Host: ${rawData.hostname} · ${sessNote} · Extracted in ${rawData.extraction_ms} ms (${genDate})`;

    const wrap = document.getElementById("active-filters");
    const chips = [];
    for (const [dim, val] of Object.entries(state.filters)) {
      if (!val) continue;
      chips.push(
        `<span class="filter-chip">${esc(dim)}: <strong>${esc(val)}</strong><button data-clear-dim="${esc(dim)}" title="Remove filter">✕</button></span>`
      );
    }
    if (state.rangePreset !== "all" && (state.dateFrom || state.dateTo)) {
      const dLabel = `${state.dateFrom || "…"} → ${state.dateTo || "…"}`;
      chips.push(
        `<span class="filter-chip">date: <strong>${esc(dLabel)}</strong><button data-clear-date="1" title="Reset date range">✕</button></span>`
      );
    }
    if (chips.length > 0) {
      chips.push(`<button id="btn-clear-all" style="font-size:11.5px;padding:3px 9px;">Clear All</button>`);
      wrap.innerHTML = chips.join("");
      wrap.hidden = false;
      wrap.querySelectorAll("[data-clear-dim]").forEach((btn) => {
        btn.addEventListener("click", () => {
          state.filters[btn.dataset.clearDim] = null;
          renderAll();
        });
      });
      const cd = wrap.querySelector("[data-clear-date]");
      if (cd) {
        cd.addEventListener("click", () => {
          applyRangePreset("all");
          renderAll();
        });
      }
      const ca = wrap.querySelector("#btn-clear-all");
      if (ca) ca.addEventListener("click", clearAllFilters);
    } else {
      wrap.hidden = true;
      wrap.innerHTML = "";
    }
  }

  function calcDirectWindowBilling(filterHarness = state.filters.harness) {
    const ledger = getEffectiveLedger();
    let sub = 0, ov = 0, promo = 0;
    for (const it of ledger) {
      if (!it.enabled) continue;
      if (filterHarness && it.harness !== filterHarness) continue;
      const frac = calcLedgerOverlapFraction(it.start_date, it.end_date);
      if (frac <= 0) continue;
      const amt = Number(it.amount_usd || 0) * frac;
      if (it.category === "subscription") sub += amt;
      else if (it.category === "overage") ov += amt;
      if (it.promo_value_usd > 0) promo += Number(it.promo_value_usd) * frac;
    }
    return { paid: sub + ov, sub, ov, promo };
  }

  function renderKPIs(grand, matchedSessionCount) {
    const grid = document.getElementById("kpi-grid");
    const reasonShare = grand.generated > 0 ? ((grand.reas / grand.generated) * 100).toFixed(1) : "0.0";
    const cacheSaved = Math.max(0, grand.noCacheCost - grand.estCost);
    const avgPerCall = grand.calls > 0 ? Math.round(grand.total / grand.calls) : 0;
    const effSessions = Math.max(grand.sessions, matchedSessionCount);

    const hasSubDimFilter = Boolean(
      state.filters.provider || state.filters.model || state.filters.project || state.filters.actor
    );
    const directWin = calcDirectWindowBilling();
    const paidUsd = hasSubDimFilter ? grand.paidCost : Math.max(grand.paidCost, directWin.paid);
    const subUsd = hasSubDimFilter ? grand.subCost : Math.max(grand.subCost, directWin.sub);
    const ovUsd = hasSubDimFilter ? grand.overageCost : Math.max(grand.overageCost, directWin.ov);
    const promoUsd = Math.max(grand.promoCost, hasSubDimFilter ? 0 : directWin.promo);
    const effPerM = grand.total > 0 ? (paidUsd / (grand.total / 1e6)).toFixed(2) : "0.00";
    const leverage = paidUsd > 0.01 ? (grand.estCost / paidUsd).toFixed(1) + "×" : "∞";

    grid.innerHTML = `
      <div class="kpi-card">
        <div class="kpi-label"><span>Total Tokens</span><span>All Buckets</span></div>
        <div class="kpi-value">${fmtTok(grand.total)}</div>
        <div class="kpi-sub">${fmtTok(grand.cr)} cached · <strong>${fmtTok(grand.active)}</strong> fresh/active</div>
      </div>

      <div class="kpi-card">
        <div class="kpi-label"><span>Generated Output</span><span>Completion</span></div>
        <div class="kpi-value">${fmtTok(grand.generated)}</div>
        <div class="kpi-sub">${fmtTok(grand.out)} text · ${fmtTok(grand.reas)} reasoning (${reasonShare}%)</div>
      </div>

      <div class="kpi-card">
        <div class="kpi-label"><span>Prompt Cache Hit</span><span>Efficiency</span></div>
        <div class="kpi-value">${fmtPct(grand.cr, grand.totalIn)}</div>
        <div class="kpi-sub">${fmtTok(grand.inp + grand.cw)} fresh in+write · saved ~${fmtUsd(cacheSaved)}</div>
      </div>

      <div class="kpi-card">
        <div class="kpi-label"><span>LLM Calls &amp; Runs</span><span>Throughput</span></div>
        <div class="kpi-value">${grand.calls.toLocaleString()}</div>
        <div class="kpi-sub">${effSessions.toLocaleString()} runs · ~${fmtTok(avgPerCall)} tok/call</div>
      </div>

      <div class="kpi-card" id="kpi-paid-card" style="cursor:pointer;border-color:rgba(92,200,255,0.35)" title="Click to inspect or customize subscriptions &amp; overages">
        <div class="kpi-label"><span>Actual Out-of-Pocket</span><span>${hasSubDimFilter ? "Attributed $" : "Paid Cash ✎"}</span></div>
        <div class="kpi-value" style="color:var(--accent)">${fmtUsd(paidUsd)}</div>
        <div class="kpi-sub"><strong>${fmtUsd(subUsd)}</strong> subs · <strong>${fmtUsd(ovUsd)}</strong> overages ($${effPerM}/1M)</div>
      </div>

      <div class="kpi-card">
        <div class="kpi-label"><span>Est. API Value</span><span>Plan Leverage</span></div>
        <div class="kpi-value">${fmtUsd(grand.estCost)}</div>
        <div class="kpi-sub"><strong>${leverage}</strong> vs paid${promoUsd > 0.01 ? ` · ${fmtUsd(promoUsd)} free promo` : ""}</div>
      </div>
    `;
    const paidCard = document.getElementById("kpi-paid-card");
    if (paidCard) paidCard.addEventListener("click", openBillingModal);
  }

  function renderBillingPanel() {
    const mixBar = document.getElementById("billing-mix-bar");
    const harnessWrap = document.getElementById("billing-harness-wrap");
    const forensicsWrap = document.getElementById("billing-forensics-wrap");
    if (!mixBar || !harnessWrap || !forensicsWrap || !rawData) return;

    const dimRows = rawData.cube.filter((r) => rowMatches(r, "harness"));
    const hasSubDim = Boolean(
      state.filters.provider || state.filters.model || state.filters.project || state.filters.actor
    );

    const hNames = ["Claude Code", "Codex", "OpenCode", "Antigravity"];
    const hMap = new Map();
    for (const nm of hNames) hMap.set(nm, emptyAgg(nm));
    for (const r of dimRows) {
      const h = r[C_HARNESS];
      if (!hMap.has(h)) hMap.set(h, emptyAgg(h));
      addRowToAgg(hMap.get(h), r);
    }

    let totSub = 0, totOv = 0, totPromo = 0;
    const hRows = [...hMap.values()].map((a) => {
      finalizeAgg(a);
      if (!hasSubDim) {
        const dw = calcDirectWindowBilling(a.name);
        a.subCost = Math.max(a.subCost, dw.sub);
        a.overageCost = Math.max(a.overageCost, dw.ov);
        a.paidCost = a.subCost + a.overageCost;
        a.promoCost = Math.max(a.promoCost, dw.promo);
      }
      if (!state.filters.harness || state.filters.harness === a.name) {
        totSub += a.subCost;
        totOv += a.overageCost;
        totPromo += a.promoCost;
      }
      return a;
    }).filter((a) => a.total > 0 || a.paidCost > 0 || a.promoCost > 0);

    hRows.sort((a, b) => b.paidCost - a.paidCost || b.estCost - a.estCost);

    const totCashAndPromo = Math.max(1e-6, totSub + totOv + totPromo);
    const barParts = [
      { label: "Subscriptions Paid", val: totSub, color: "#38bdf8" },
      { label: "Overages / Credit Packs Paid", val: totOv, color: "#f59e0b" },
      { label: "Free Promo Credits Absorbed ($0 Paid)", val: totPromo, color: "#34d399" },
    ];
    mixBar.innerHTML = barParts
      .filter((p) => p.val > 0.005)
      .map((p) => {
        const pct = (p.val / totCashAndPromo) * 100;
        return `<div class="comp-seg" style="width:${Math.max(pct, 1).toFixed(2)}%;background:${p.color}" title="${p.label}: ${fmtUsd(p.val)}"></div>`;
      })
      .join("");

    const legendStrip = `
      <div style="display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--text-muted);margin:-4px 0 12px;">
        <span><span class="swatch" style="background:#38bdf8;margin-right:5px;"></span>Subscriptions: <strong class="num" style="color:var(--text)">${fmtUsd(totSub)}</strong></span>
        <span><span class="swatch" style="background:#f59e0b;margin-right:5px;"></span>Overages / Credits: <strong class="num" style="color:var(--text)">${fmtUsd(totOv)}</strong></span>
        <span><span class="swatch" style="background:#34d399;margin-right:5px;"></span>Promo Absorbed ($0 paid): <strong class="num" style="color:var(--text)">${fmtUsd(totPromo)}</strong></span>
      </div>
    `;

    const trs = hRows
      .map((a) => {
        const col = PALETTE.harness[a.name] || "var(--accent)";
        const sel = state.filters.harness === a.name;
        const lev = a.paidCost > 0.01 ? (a.estCost / a.paidCost).toFixed(1) + "×" : a.estCost > 0 ? "Free / $0" : "—";
        const effRate = a.total > 0 ? "$" + (a.paidCost / (a.total / 1e6)).toFixed(2) : "—";
        return `
          <tr class="clickable ${sel ? "selected-row" : ""}" data-bill-harness="${esc(a.name)}">
            <td><span class="dot" style="background:${col};margin-right:7px;"></span><strong>${esc(a.name)}</strong></td>
            <td class="right num">${fmtUsd(a.subCost)}</td>
            <td class="right num">${fmtUsd(a.overageCost)}</td>
            <td class="right num" style="color:${a.promoCost > 0 ? "#34d399" : "var(--text-dim)"}">${a.promoCost > 0 ? fmtUsd(a.promoCost) : "—"}</td>
            <td class="right num"><strong>${fmtUsd(a.paidCost)}</strong></td>
            <td class="right num">${fmtUsd(a.estCost)}</td>
            <td class="right num">${effRate}</td>
            <td class="right num" style="color:var(--accent)"><strong>${lev}</strong></td>
          </tr>
        `;
      })
      .join("");

    harnessWrap.innerHTML = `
      ${legendStrip}
      <table class="data-table">
        <thead>
          <tr>
            <th>Harness</th>
            <th class="right">Subscriptions</th>
            <th class="right">Overages</th>
            <th class="right">Promo ($0 Paid)</th>
            <th class="right">Actual Paid</th>
            <th class="right">Est. API Value</th>
            <th class="right">Real $/1M</th>
            <th class="right">Leverage</th>
          </tr>
        </thead>
        <tbody>${trs}</tbody>
      </table>
    `;
    harnessWrap.querySelectorAll("[data-bill-harness]").forEach((tr) => {
      tr.addEventListener("click", () => toggleFilter("harness", tr.dataset.billHarness));
    });

    // Forensic cards on the right (rendered purely from dynamic extracted telemetry + effective ledger)
    const bill = rawData.billing || {};
    const cw = bill.codex_wallet || {};
    const ct = bill.claude_telemetry || {};
    const ot = bill.opencode_telemetry || {};
    const topCodexProj = Object.entries(cw.burns_by_project_usd || {})
      .slice(0, 3)
      .map(([p, v]) => `${esc(p)} (${fmtUsd(v)})`)
      .join(", ");
    const aug = ct.aug_snapshot || null;
    const codexSegSummary = (cw.plan_segments || [])
      .map((s) => `<strong>${esc(s.plan_type)}</strong> (${esc(s.start_date)}..${esc(s.end_date)})`)
      .join(" → ");
    const claudeSnapText = aug
      ? `Snapshot (${esc(aug.date || "cached")}) logged <strong>$${Number(aug.used_credits_usd || 0).toFixed(2)}/$${Number(aug.monthly_limit_usd || 0).toFixed(0)}</strong> Extra Usage` +
        (aug.seven_day_pct != null ? ` (<code>seven_day: ${aug.seven_day_pct}%</code>)` : "") +
        ". "
      : "";
    const claudeStepText = ct.inferred_upgrade_week
      ? `Week of <strong>${esc(ct.inferred_upgrade_week)}</strong> stepped from ~${fmtUsd(ct.pre_upgrade_weekly_api_usd || 0)}/wk to ~${fmtUsd(ct.post_upgrade_weekly_api_usd || 0)}/wk API-equiv. `
      : "";
    const effLedger = getEffectiveLedger().filter((it) => it.enabled);
    const agPaidSum = effLedger
      .filter((it) => it.harness === "Antigravity")
      .reduce((acc, it) => acc + Number(it.amount_usd || 0), 0);

    forensicsWrap.innerHTML = `
      <div class="forensic-card">
        <div class="forensic-head">
          <span><span class="dot" style="background:var(--c-codex);margin-right:6px;"></span>Codex Wallet &amp; Plan Telemetry</span>
          <span class="src-pill src-extracted">Extracted Exact</span>
        </div>
        <div style="color:var(--text-muted)">
          <code>rate_limits.plan_type</code>: ${codexSegSummary || "none logged"}.
          <code>credits.balance</code> logs <strong>${(cw.purchases || []).length} × 500-credit packs = ${fmtUsd(cw.overage_billed_usd || 0)}</strong> billed:
          <strong>${fmtUsd(cw.overage_burned_usd || 0)}</strong> burned${topCodexProj ? ` (${topCodexProj})` : ""} +
          <strong>${fmtUsd(cw.overage_remaining_usd || 0)}</strong> (${(cw.credits_remaining || 0).toFixed(0)} cr) remaining in wallet.
        </div>
      </div>

      <div class="forensic-card">
        <div class="forensic-head">
          <span><span class="dot" style="background:var(--c-claude);margin-right:6px;"></span>Claude Code Plan &amp; Extra Usage</span>
          <span class="src-pill src-inferred">Extracted + Inferred</span>
        </div>
        <div style="color:var(--text-muted)">
          ${ct.subscription_created_date ? `<code>subscriptionCreatedAt</code>: <strong>${esc(ct.subscription_created_date)}</strong>. ` : ""}${claudeSnapText}${claudeStepText}${ct.fable_overage_consent ? `Overage consent active (${(ct.fable_calls || 0).toLocaleString()} scoped frontier calls).` : ""}
        </div>
      </div>

      <div class="forensic-card">
        <div class="forensic-head">
          <span>
            <span class="dot" style="background:var(--c-opencode);margin-right:4px;"></span>
            <span class="dot" style="background:var(--c-antigravity);margin-right:6px;"></span>OpenCode &amp; Antigravity
          </span>
          <span class="src-pill src-user_calibrated">Ledger</span>
        </div>
        <div style="color:var(--text-muted)">
          <strong>OpenCode:</strong> ${
            (ot.promo_anthropic_usd || 0) > 0
              ? `<strong>${fmtUsd(ot.session_header_promo_usd || ot.promo_anthropic_usd || 0)}</strong> promotional credits absorbed ($0 paid)`
              : `<strong>${fmtUsd(ot.direct_paid_usd || 0)}</strong> direct API spend`
          }; local self-hosted GPU runs at $0.
          <strong>Antigravity:</strong> <strong>${fmtUsd(agPaidSum)}</strong> attributed across active ledger periods.
        </div>
      </div>
    `;
  }

  // -------------------------------------------------------------------------
  // SVG Stacked Timeline Chart
  // -------------------------------------------------------------------------

  function colorsForSeries(stackMode, keys) {
    return seriesColorMap(stackMode, keys);
  }

  // Hourly bars beyond this many days get too thin to read; show the most recent window instead
  const HOURLY_MAX_DAYS = 31;

  function addDaysISO(dateStr, days) {
    const [y, m, d] = dateStr.split("-").map(Number);
    const dtObj = new Date(Date.UTC(y, m - 1, d + days));
    return dtObj.toISOString().slice(0, 10);
  }

  function dateSpanDays(minD, maxD) {
    return Math.round((Date.parse(maxD) - Date.parse(minD)) / 86400000);
  }

  function renderTimeline(filteredRows, filteredHourly) {
    const svg = document.getElementById("timeline-svg");
    const legendEl = document.getElementById("timeline-legend");
    const wrap = document.getElementById("timeline-wrap");
    const tooltip = document.getElementById("chart-tooltip");
    const subEl = document.getElementById("timeline-subtitle");

    const width = Math.max(360, wrap.clientWidth || 900);
    const height = Math.max(220, wrap.clientHeight || 290);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.classList.remove("series-focus");

    const metric = state.metric;
    let grain = state.chartGrain;
    const notes = [];
    let sourceRows = filteredRows;

    if (grain === "hour") {
      sourceRows = filteredHourly || [];
      let minD = "9999-99-99", maxD = "0000-00-00";
      for (const r of sourceRows) {
        const d = r[C_DATE];
        if (d && d !== "unknown") {
          if (d < minD) minD = d;
          if (d > maxD) maxD = d;
        }
      }
      let cutoff = "";
      if (minD <= maxD && dateSpanDays(minD, maxD) >= HOURLY_MAX_DAYS) {
        cutoff = addDaysISO(maxD, -(HOURLY_MAX_DAYS - 1));
        sourceRows = sourceRows.filter((r) => r[C_DATE] >= cutoff);
        notes.push(`Showing the latest ${HOURLY_MAX_DAYS} days hourly (from ${cutoff}) — narrow the date range to inspect earlier hours`);
      }
      // Volume without hour-level timestamps (e.g. pre-cleanup Claude history) cannot be placed on the hour axis
      let dailyTot = 0, hourlyTot = 0;
      for (const r of filteredRows) {
        if (cutoff && !(r[C_DATE] >= cutoff)) continue;
        dailyTot += metricFromRow(r, metric);
      }
      for (const r of sourceRows) hourlyTot += metricFromRow(r, metric);
      if (dailyTot > 0 && hourlyTot < dailyTot * 0.995) {
        notes.push(`${fmtPct(dailyTot - hourlyTot, dailyTot)} of this volume has no hour-level timestamps (e.g. pre-cleanup Claude history) and is omitted`);
      }
    } else if (grain === "day") {
      // Auto-upgrade to weekly grain if daily span > 366 days so long histories never truncate
      let minD = "9999-99-99", maxD = "0000-00-00";
      for (const r of filteredRows) {
        const d = r[C_DATE];
        if (d && d !== "unknown") {
          if (d < minD) minD = d;
          if (d > maxD) maxD = d;
        }
      }
      if (minD <= maxD && dateSpanDays(minD, maxD) > 366) {
        grain = "week";
        notes.push("Auto-grouped by week (>1 year span)");
      }
    }

    if (subEl) {
      const hint =
        grain === "hour"
          ? "Local-time hourly buckets · Hover a legend item to highlight it, click to filter · Click a bar to isolate that day"
          : "Hover a legend item to highlight it, click to filter · Click a bar to isolate a date/week";
      subEl.textContent = [...notes, hint].join(" · ");
    }

    if (!sourceRows.length) {
      const msg = grain === "hour" && filteredRows.length
        ? "No hour-level activity for the current filters (re-run token-stats to index hourly timestamps)."
        : "No token activity matches the current filters.";
      svg.innerHTML = `<text x="${width / 2}" y="${height / 2}" text-anchor="middle" fill="var(--text-muted)">${esc(msg)}</text>`;
      legendEl.innerHTML = "";
      return;
    }

    const stackMode = state.chartStack;
    const pad2 = (n) => String(n).padStart(2, "0");
    const seriesTotals = new Map();
    const bucketed = new Map();

    for (const r of sourceRows) {
      const rawDate = r[C_DATE];
      if (!rawDate || rawDate === "unknown") continue;
      const period =
        grain === "hour" ? `${rawDate}T${pad2(r[C_HOUR])}` : grain === "week" ? weekStartISO(rawDate) : rawDate;
      if (!bucketed.has(period)) bucketed.set(period, new Map());
      const pMap = bucketed.get(period);

      if (stackMode === "bucket") {
        const model = r[C_MODEL];
        const rb = getRowBilling(r);
        const parts =
          metric === "paid_cost"
            ? {
                Subscriptions: rb.sub,
                "Overages / Credits": rb.ov,
              }
            : metric === "est_cost"
            ? {
                "Cache Read": (r[C_CR] * (getModelRate(model).cr || 0)) / 1e6,
                "Cache Write": (r[C_CW] * (getModelRate(model).cw || 0)) / 1e6,
                "Uncached Input": (r[C_IN] * (getModelRate(model).in || 0)) / 1e6,
                "Text Output": (r[C_OUT] * (getModelRate(model).out || 0)) / 1e6,
                "Reasoning": (r[C_REAS] * (getModelRate(model).out || 0)) / 1e6,
              }
            : metric === "active"
            ? {
                "Cache Write": r[C_CW],
                "Uncached Input": r[C_IN],
                "Text Output": r[C_OUT],
                "Reasoning": r[C_REAS],
              }
            : metric === "generated"
            ? {
                "Text Output": r[C_OUT],
                "Reasoning": r[C_REAS],
              }
            : {
                "Cache Read": r[C_CR],
                "Cache Write": r[C_CW],
                "Uncached Input": r[C_IN],
                "Text Output": r[C_OUT],
                "Reasoning": r[C_REAS],
              };
        for (const [bk, bv] of Object.entries(parts)) {
          if (bv <= 0) continue;
          pMap.set(bk, (pMap.get(bk) || 0) + bv);
          seriesTotals.set(bk, (seriesTotals.get(bk) || 0) + bv);
        }
      } else {
        const colIdx =
          stackMode === "harness"
            ? C_HARNESS
            : stackMode === "provider"
            ? C_PROVIDER
            : stackMode === "project"
            ? C_PROJECT
            : stackMode === "actor"
            ? C_ACTOR
            : C_MODEL;
        const sk = r[colIdx];
        const val = metricFromRow(r, metric);
        if (val <= 0) continue;
        pMap.set(sk, (pMap.get(sk) || 0) + val);
        seriesTotals.set(sk, (seriesTotals.get(sk) || 0) + val);
      }
    }

    const rankedSeries = [...seriesTotals.entries()].sort((a, b) => b[1] - a[1]);
    // Fold the tail into "Other" only when it holds 2+ series (a lone extra series keeps its own name)
    const maxKeep = rankedSeries.length > TOP_SERIES + 1 ? TOP_SERIES : rankedSeries.length;
    const topKeys = rankedSeries.slice(0, maxKeep).map((e) => e[0]);
    const topSet = new Set(topKeys);
    const hasOther = rankedSeries.length > maxKeep;
    const finalKeys = hasOther ? [...topKeys, "Other"] : topKeys;

    const sortedPeriods = [...bucketed.keys()].sort();
    const periods = [];
    if (sortedPeriods.length > 0) {
      const first = sortedPeriods[0];
      const last = sortedPeriods[sortedPeriods.length - 1];
      const [sy, sm, sd] = first.slice(0, 10).split("-").map(Number);
      const [ey, em, ed] = last.slice(0, 10).split("-").map(Number);
      if (grain === "hour") {
        // Whole local days, midnight to 23:00, so a single isolated day always shows 24 bars
        let cur = Date.UTC(sy, sm - 1, sd, 0);
        const end = Date.UTC(ey, em - 1, ed, 23);
        while (cur <= end && periods.length < 24 * (HOURLY_MAX_DAYS + 1)) {
          periods.push(new Date(cur).toISOString().slice(0, 13));
          cur += 3600000;
        }
      } else {
        const cur = new Date(Date.UTC(sy, sm - 1, sd));
        const end = new Date(Date.UTC(ey, em - 1, ed));
        const stepDays = grain === "week" ? 7 : 1;
        while (cur <= end && periods.length < 1500) {
          periods.push(cur.toISOString().slice(0, 10));
          cur.setUTCDate(cur.getUTCDate() + stepDays);
        }
      }
    }

    const periodData = periods.map((p) => {
      const src = bucketed.get(p) || new Map();
      const vals = {};
      let sum = 0;
      for (const k of finalKeys) vals[k] = 0;
      for (const [k, v] of src.entries()) {
        const target = topSet.has(k) ? k : "Other";
        vals[target] = (vals[target] || 0) + v;
        sum += v;
      }
      return { period: p, vals, total: sum };
    });

    const maxTotal = Math.max(...periodData.map((d) => d.total), 1e-9);
    const padL = 62, padR = 14, padT = 14, padB = 30;
    const plotW = Math.max(100, width - padL - padR);
    const plotH = Math.max(100, height - padT - padB);

    const n = periodData.length || 1;
    const stepX = plotW / n;
    const barW = Math.max(1, Math.min(34, stepX * (grain === "hour" ? 0.82 : 0.78)));

    let svgOut = "";
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const frac = i / ticks;
      const y = padT + plotH - frac * plotH;
      const tickVal = maxTotal * frac;
      svgOut += `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${width - padR}" y2="${y.toFixed(1)}" stroke="var(--border)" stroke-dasharray="${i === 0 ? "none" : "3,3"}" />`;
      svgOut += `<text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="var(--text-dim)" font-size="11" font-family="var(--mono)">${fmtMetric(tickVal, metric)}</text>`;
    }

    // Faint day separators in hourly view
    if (grain === "hour" && n > 24) {
      periodData.forEach((d, idx) => {
        if (idx === 0 || !d.period.endsWith("T00")) return;
        const x = padL + idx * stepX;
        svgOut += `<line x1="${x.toFixed(1)}" y1="${padT}" x2="${x.toFixed(1)}" y2="${padT + plotH}" stroke="var(--border)" stroke-width="1" />`;
      });
    }

    const colorMap = colorsForSeries(stackMode, finalKeys);
    const seriesIdx = {};
    finalKeys.forEach((k, i) => {
      seriesIdx[k] = i;
    });

    periodData.forEach((d, idx) => {
      const x = padL + idx * stepX + (stepX - barW) / 2;
      let yCursor = padT + plotH;
      for (const k of finalKeys) {
        const v = d.vals[k] || 0;
        if (v <= 0) continue;
        const h = (v / maxTotal) * plotH;
        const drawH = Math.min(yCursor - padT, Math.max(0.8, h));
        if (drawH <= 0) continue;
        yCursor -= drawH;
        // data-off: distance from this segment's bottom edge to the baseline (used by legend focus)
        const off = padT + plotH - (yCursor + drawH);
        svgOut += `<rect class="seg" data-s="${seriesIdx[k]}" data-off="${off.toFixed(2)}" x="${x.toFixed(2)}" y="${yCursor.toFixed(2)}" width="${barW.toFixed(2)}" height="${drawH.toFixed(2)}" fill="${colorMap[k]}" rx="${barW > 4 ? 1.2 : 0}" />`;
      }
      svgOut += `<rect class="bar-hit" data-idx="${idx}" x="${(padL + idx * stepX).toFixed(2)}" y="${padT}" width="${stepX.toFixed(2)}" height="${plotH}" fill="transparent" style="cursor:pointer" />`;
    });

    const labelAt = (idx, txt) => {
      const cx = padL + idx * stepX + stepX / 2;
      svgOut += `<text x="${cx.toFixed(1)}" y="${height - 8}" text-anchor="middle" fill="var(--text-dim)" font-size="11" font-family="var(--mono)">${esc(txt)}</text>`;
    };
    if (grain === "hour") {
      // Snap label spacing to clock-friendly steps so labels land on 00/06/12/18 etc.
      const minPx = 58;
      const stepsH = [1, 2, 3, 6, 12, 24, 48, 72, 168, 336];
      const stepH = stepsH.find((s) => s * stepX >= minPx) || 336;
      periodData.forEach((d, idx) => {
        if (idx % stepH !== 0) return;
        const hh = d.period.slice(11, 13);
        const isMidnight = hh === "00";
        labelAt(idx, isMidnight && (stepH >= 24 || n > 24) ? d.period.slice(5, 10) : `${hh}:00`);
      });
    } else {
      const labelEvery = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(plotW / 78))));
      periodData.forEach((d, idx) => {
        // Always label the final bar, unless it would collide with the previous regular label
        const isLast = idx === n - 1 && ((n - 1) % labelEvery) * stepX >= 60;
        if (idx % labelEvery !== 0 && !isLast) return;
        labelAt(idx, d.period.slice(5));
      });
    }

    svg.innerHTML = svgOut;

    const periodTitle = (p) => {
      if (grain === "week") return "Week of " + p;
      if (grain === "hour") {
        const hNum = Number(p.slice(11, 13));
        return `${p.slice(0, 10)} · ${pad2(hNum)}:00–${pad2((hNum + 1) % 24)}:00`;
      }
      return p;
    };

    svg.querySelectorAll(".bar-hit").forEach((rect) => {
      rect.addEventListener("mouseenter", () => {
        rect.setAttribute("fill", "rgba(127,127,127,0.07)");
      });
      rect.addEventListener("mousemove", (ev) => {
        const d = periodData[Number(rect.dataset.idx)];
        if (!d) return;
        const rowsHtml = finalKeys
          .filter((k) => d.vals[k] > 0)
          .reverse()
          .map(
            (k) => `
            <div class="tt-row">
              <span><span class="swatch" style="background:${colorMap[k]};margin-right:6px;"></span>${esc(ACTOR_LABELS[k] || k)}</span>
              <span class="num">${fmtMetric(d.vals[k], metric)}</span>
            </div>`
          )
          .join("");
        tooltip.innerHTML = `
          <div class="tt-title">
            <span>${esc(periodTitle(d.period))}</span>
            <span class="num">${fmtMetric(d.total, metric)}</span>
          </div>
          ${rowsHtml || '<div style="color:var(--text-dim)">No activity</div>'}
        `;
        positionTooltip(tooltip, ev);
      });
      rect.addEventListener("mouseleave", () => {
        rect.setAttribute("fill", "transparent");
        tooltip.hidden = true;
      });
      rect.addEventListener("click", () => {
        const d = periodData[Number(rect.dataset.idx)];
        if (!d) return;
        if (grain === "week") {
          isolateDates(d.period, addDaysISO(d.period, 6));
        } else {
          const day = d.period.slice(0, 10);
          isolateDates(day, day);
        }
        tooltip.hidden = true;
      });
    });

    const otherSum = rankedSeries.slice(maxKeep).reduce((s, e) => s + e[1], 0);
    const otherCount = rankedSeries.length - maxKeep;
    legendEl.innerHTML = finalKeys
      .map((k) => {
        const tot = k === "Other" ? otherSum : (seriesTotals.get(k) || 0);
        const clickable = k !== "Other" && stackMode !== "bucket";
        const label = k === "Other" ? `Other (${otherCount})` : ACTOR_LABELS[k] || k;
        return `
          <span class="legend-item" data-s="${seriesIdx[k]}" ${clickable ? `data-filter-key="${esc(k)}"` : ""}>
            <span class="swatch" style="background:${colorMap[k]}"></span>
            <span>${esc(label)}</span>
            <strong class="num" style="color:var(--text)">${fmtMetric(tot, metric)}</strong>
          </span>
        `;
      })
      .join("");

    // Legend hover: spotlight one series (dropped onto the baseline so its own shape is readable),
    // fade the rest of the chart and the other legend entries
    const segs = svg.querySelectorAll("rect.seg");
    const legendItems = legendEl.querySelectorAll(".legend-item");
    const setFocus = (si) => {
      svg.classList.toggle("series-focus", si !== null);
      segs.forEach((r) => {
        const on = si !== null && r.dataset.s === si;
        r.classList.toggle("is-focus", on);
        r.style.transform = on && Number(r.dataset.off) > 0 ? `translateY(${r.dataset.off}px)` : "";
      });
      legendItems.forEach((el) => el.classList.toggle("is-dim", si !== null && el.dataset.s !== si));
    };
    legendItems.forEach((el) => {
      el.addEventListener("mouseenter", () => setFocus(el.dataset.s));
      el.addEventListener("mouseleave", () => setFocus(null));
    });

    legendEl.querySelectorAll("[data-filter-key]").forEach((el) => {
      el.addEventListener("click", () => {
        toggleFilter(stackMode, el.dataset.filterKey);
      });
    });
  }

  function positionTooltip(tooltip, ev) {
    tooltip.hidden = false;
    const tw = tooltip.offsetWidth || 250;
    const th = tooltip.offsetHeight || 140;
    const tx = Math.max(12, Math.min(window.innerWidth - tw - 12, ev.clientX + 14));
    const ty = Math.max(12, Math.min(window.innerHeight - th - 12, ev.clientY - 24));
    tooltip.style.left = tx + "px";
    tooltip.style.top = ty + "px";
  }

  function isolateDates(from, to) {
    state.rangePreset = "custom";
    document.querySelectorAll("#range-presets .seg-btn").forEach((b) => b.classList.remove("active"));
    state.dateFrom = from;
    state.dateTo = to;
    document.getElementById("date-from").value = state.dateFrom;
    document.getElementById("date-to").value = state.dateTo;
    renderAll();
  }

  // -------------------------------------------------------------------------
  // GitHub-style Daily Activity Calendar
  // -------------------------------------------------------------------------

  function renderActivityCalendar(filteredRows) {
    const wrap = document.getElementById("calendar-wrap");
    const statsEl = document.getElementById("calendar-stats");
    const tooltip = document.getElementById("chart-tooltip");
    if (!wrap) return;
    const metric = state.metric;

    const dayVals = new Map();
    const dayCalls = new Map();
    let minD = "9999-99-99", maxD = "0000-00-00";
    for (const r of filteredRows) {
      const d = r[C_DATE];
      if (!d || d === "unknown") continue;
      const v = metricFromRow(r, metric);
      dayVals.set(d, (dayVals.get(d) || 0) + v);
      dayCalls.set(d, (dayCalls.get(d) || 0) + r[C_CALLS]);
      if (d < minD) minD = d;
      if (d > maxD) maxD = d;
    }

    const from = state.dateFrom || (minD <= maxD ? minD : "");
    const to = state.dateTo || (minD <= maxD ? maxD : "");
    if (!from || !to || from > to) {
      wrap.innerHTML = `<div class="empty-note">No token activity matches the current filters.</div>`;
      statsEl.innerHTML = "";
      return;
    }

    const gridStart = weekStartISO(from);
    const totalDays = dateSpanDays(gridStart, to) + 1;
    const weeks = Math.max(1, Math.ceil(totalDays / 7));

    // Quartile intensity levels over active days (robust to a few huge burst days)
    const nonZero = [...dayVals.entries()]
      .filter(([d, v]) => v > 0 && d >= from && d <= to)
      .map(([, v]) => v)
      .sort((a, b) => a - b);
    const q = (p) => (nonZero.length ? nonZero[Math.min(nonZero.length - 1, Math.floor(p * nonZero.length))] : 0);
    const thresholds = [q(0.25), q(0.5), q(0.75)];
    const levelOf = (v) => {
      if (!(v > 0)) return 0;
      if (v <= thresholds[0]) return 1;
      if (v <= thresholds[1]) return 2;
      if (v <= thresholds[2]) return 3;
      return 4;
    };

    const labelW = 30, topH = 18, gap = 3;
    const availW = Math.max(240, (wrap.clientWidth || 900) - labelW - 4);
    const cell = Math.max(10, Math.min(24, Math.floor(availW / weeks) - gap));
    const pitch = cell + gap;
    const svgW = labelW + weeks * pitch;
    const svgH = topH + 7 * pitch;
    const heat = PALETTE.heat;

    let out = "";
    const dowLbl = ["Mon", "", "Wed", "", "Fri", "", ""];
    dowLbl.forEach((lbl, i) => {
      if (!lbl) return;
      out += `<text x="${labelW - 6}" y="${topH + i * pitch + cell - 1}" text-anchor="end" font-size="10" fill="var(--text-dim)">${lbl}</text>`;
    });

    let lastMonth = "";
    let lastMonthX = -1;
    for (let w = 0; w < weeks; w++) {
      const x = labelW + w * pitch;
      // Month label above the first column of each month (by the column's last visible day)
      const colEnd = addDaysISO(gridStart, w * 7 + 6);
      const ym = (colEnd > to ? to : colEnd).slice(0, 7);
      if (ym !== lastMonth) {
        if (x - lastMonthX >= 28 || lastMonthX < 0) {
          const [yy, mm] = ym.split("-").map(Number);
          const showYear = mm === 1 || lastMonthX < 0;
          out += `<text x="${x}" y="${topH - 6}" font-size="10" fill="var(--text-dim)">${MONTH_NAMES[mm - 1]}${showYear ? " " + yy : ""}</text>`;
          lastMonthX = x;
        }
        lastMonth = ym;
      }
      for (let dow = 0; dow < 7; dow++) {
        const d = addDaysISO(gridStart, w * 7 + dow);
        if (d > to) break;
        const y = topH + dow * pitch;
        if (d < from) continue;
        const v = dayVals.get(d) || 0;
        const lvl = levelOf(v);
        out += `<rect class="cal-cell" data-date="${d}" x="${x}" y="${y}" width="${cell}" height="${cell}" rx="2.5" fill="${heat[lvl]}" />`;
      }
    }

    wrap.innerHTML = `<svg class="calendar-svg" width="${svgW}" height="${svgH}" viewBox="0 0 ${svgW} ${svgH}" role="img" aria-label="Daily activity calendar">${out}</svg>`;

    const dowFull = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    wrap.querySelectorAll(".cal-cell").forEach((el) => {
      el.addEventListener("mousemove", (ev) => {
        const d = el.dataset.date;
        const v = dayVals.get(d) || 0;
        const [y, m, dd] = d.split("-").map(Number);
        const wd = dowFull[new Date(Date.UTC(y, m - 1, dd)).getUTCDay()];
        tooltip.innerHTML = `
          <div class="tt-title"><span>${wd} ${esc(d)}</span><span class="num">${v > 0 ? fmtMetric(v, metric) : "—"}</span></div>
          <div class="tt-row"><span>${v > 0 ? "API calls" : "No activity"}</span><span class="num">${v > 0 ? (dayCalls.get(d) || 0).toLocaleString() : ""}</span></div>
        `;
        positionTooltip(tooltip, ev);
      });
      el.addEventListener("mouseleave", () => {
        tooltip.hidden = true;
      });
      el.addEventListener("click", () => {
        tooltip.hidden = true;
        isolateDates(el.dataset.date, el.dataset.date);
      });
    });

    // Streaks & summary over the visible range
    let activeDays = 0, rangeDays = 0, longest = 0, run = 0, busiest = null, sumActive = 0;
    for (let d = from; d <= to; d = addDaysISO(d, 1)) {
      rangeDays++;
      const v = dayVals.get(d) || 0;
      if (v > 0) {
        activeDays++;
        sumActive += v;
        run++;
        longest = Math.max(longest, run);
        if (!busiest || v > busiest[1]) busiest = [d, v];
      } else {
        run = 0;
      }
      if (rangeDays > 20000) break;
    }
    const current = run;
    const legendCells = heat
      .map((c) => `<span class="cal-swatch" style="background:${c}"></span>`)
      .join("");
    statsEl.innerHTML = `
      <span>Active days <strong class="num">${activeDays}</strong> / ${rangeDays}</span>
      <span>Longest streak <strong class="num">${longest}</strong> d</span>
      <span>Current streak <strong class="num">${current}</strong> d</span>
      <span>Avg / active day <strong class="num">${fmtMetric(activeDays ? sumActive / activeDays : 0, metric)}</strong></span>
      ${busiest ? `<span>Busiest <strong class="num">${esc(busiest[0])}</strong> (${fmtMetric(busiest[1], metric)})</span>` : ""}
      <span class="cal-legend">Less ${legendCells} More</span>
    `;
  }

  // -------------------------------------------------------------------------
  // Token Anatomy & Actor Role / Day-of-Week
  // -------------------------------------------------------------------------

  function renderAnatomyAndRoles(grand, filteredRows, actorDimRows, filteredHourly) {
    const barEl = document.getElementById("anatomy-bar");
    const bucketsEl = document.getElementById("anatomy-buckets");
    const tot = Math.max(1, grand.total);

    const buckets = [
      { label: "Cache Read", val: grand.cr, color: "var(--t-cread-bright)", desc: "Reused prompt prefix" },
      { label: "Cache Write", val: grand.cw, color: "var(--t-cwrite)", desc: "Newly written cache" },
      { label: "Uncached Input", val: grand.inp, color: "var(--t-uncached)", desc: "Fresh prompt tokens" },
      { label: "Text Output", val: grand.out, color: "var(--t-output)", desc: "Visible completion" },
      { label: "Reasoning", val: grand.reas, color: "var(--t-reason)", desc: "Thinking / CoT tokens" },
    ];

    barEl.innerHTML = buckets
      .map((b) => {
        const pct = (b.val / tot) * 100;
        if (pct <= 0) return "";
        return `<div class="comp-seg" style="width:${Math.max(pct, 0.35).toFixed(2)}%;background:${b.color}" title="${b.label}: ${fmtTok(b.val)} (${pct.toFixed(2)}%)"></div>`;
      })
      .join("");

    bucketsEl.innerHTML = buckets
      .map((b) => {
        const pct = ((b.val / tot) * 100).toFixed(2);
        return `
          <div class="bucket-item">
            <div class="bucket-head">
              <span class="swatch" style="background:${b.color}"></span>
              <span>${b.label}</span>
            </div>
            <div class="bucket-val">${fmtTok(b.val)}</div>
            <div class="bucket-pct">${pct}% · ${b.desc}</div>
          </div>
        `;
      })
      .join("");

    // Actor roles (ignores actor's own filter so sibling roles stay visible for 1-click switching)
    const roleMap = new Map();
    for (const r of actorDimRows) {
      const ak = r[C_ACTOR];
      if (!roleMap.has(ak)) roleMap.set(ak, emptyAgg(ak));
      addRowToAgg(roleMap.get(ak), r);
    }
    const roles = [...roleMap.values()].map(finalizeAgg).sort((a, b) => b.primary - a.primary);
    const roleSum = roles.reduce((s, x) => s + x.primary, 0) || 1;

    const actorEl = document.getElementById("actor-rows");
    actorEl.innerHTML = roles
      .map((r) => {
        const pct = (r.primary / roleSum) * 100;
        const col = PALETTE.actor[r.name] || "var(--accent)";
        const isSel = state.filters.actor === r.name;
        return `
          <div class="role-row ${isSel ? "selected-row" : ""}" data-actor="${esc(r.name)}">
            <div class="role-top">
              <span>
                <span class="dot" style="background:${col};margin-right:6px;"></span>
                <strong>${esc(ACTOR_LABELS[r.name] || r.name)}</strong>
                <span style="color:var(--text-dim);font-size:11.5px;margin-left:6px;">${r.calls.toLocaleString()} calls</span>
              </span>
              <span class="num"><strong>${fmtMetric(r.primary)}</strong> <span style="color:var(--text-muted)">(${pct.toFixed(1)}%)</span></span>
            </div>
            <div class="mini-track">
              <div class="mini-fill" style="width:${pct.toFixed(1)}%;background:${col}"></div>
            </div>
          </div>
        `;
      })
      .join("");

    actorEl.querySelectorAll("[data-actor]").forEach((el) => {
      el.addEventListener("click", () => toggleFilter("actor", el.dataset.actor));
    });

    // Day-of-Week profile (Mon..Sun)
    const dowNames = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    const dowVals = [0, 0, 0, 0, 0, 0, 0];
    for (const r of filteredRows) {
      const d = r[C_DATE];
      if (!d || d === "unknown") continue;
      const [y, m, day] = d.split("-").map(Number);
      const jsDay = new Date(Date.UTC(y, m - 1, day)).getUTCDay();
      const idx = jsDay === 0 ? 6 : jsDay - 1;
      dowVals[idx] += metricFromRow(r);
    }
    const maxDow = Math.max(...dowVals, 1);
    document.getElementById("dow-grid").innerHTML = dowNames
      .map((nm, i) => {
        const pct = Math.round((dowVals[i] / maxDow) * 100);
        return `
          <div class="dow-col" title="${nm}: ${fmtMetric(dowVals[i])}">
            <div class="dow-name">${nm}</div>
            <div class="dow-val">${fmtMetric(dowVals[i])}</div>
            <div class="mini-track" style="margin-top:5px;height:4px;">
              <div class="mini-fill" style="width:${pct}%;background:var(--accent)"></div>
            </div>
          </div>
        `;
      })
      .join("");

    // Hour-of-day profile (local time, 00..23)
    const hodVals = new Array(24).fill(0);
    for (const r of filteredHourly || []) {
      const h = r[C_HOUR];
      if (h >= 0 && h < 24) hodVals[h] += metricFromRow(r);
    }
    const maxHod = Math.max(...hodVals, 0);
    const hodEl = document.getElementById("hod-grid");
    if (hodEl) {
      if (maxHod <= 0) {
        hodEl.innerHTML = `<div class="hod-head"><span>Hour of day (local)</span><span>No hour-level data</span></div>`;
      } else {
        const peak = hodVals.indexOf(maxHod);
        const p2 = (x) => String(x).padStart(2, "0");
        hodEl.innerHTML = `
          <div class="hod-head">
            <span>Hour of day (local)</span>
            <span>Peak <strong class="num">${p2(peak)}:00–${p2((peak + 1) % 24)}:00</strong> · ${fmtMetric(maxHod)}</span>
          </div>
          <div class="hod-bars">
            ${hodVals
              .map((v, h) => {
                const pct = maxHod > 0 ? (v / maxHod) * 100 : 0;
                return `<div class="hod-col" title="${p2(h)}:00–${p2((h + 1) % 24)}:00 · ${fmtMetric(v)}"><div class="hod-fill" style="height:${v > 0 ? Math.max(3, pct).toFixed(1) : 0}%"></div></div>`;
              })
              .join("")}
          </div>
          <div class="hod-axis">${hodVals.map((_, h) => `<span>${h % 3 === 0 ? p2(h) : ""}</span>`).join("")}</div>
        `;
      }
    }
  }

  // -------------------------------------------------------------------------
  // Breakdown Tables
  // -------------------------------------------------------------------------

  function inlineMixBar(agg, maxPrimary) {
    const scalePct = maxPrimary > 0 ? Math.max(2, (agg.primary / maxPrimary) * 100) : 0;
    const tot = Math.max(1, agg.total);
    const pIn = (agg.inp / tot) * 100;
    const pCw = (agg.cw / tot) * 100;
    const pCr = (agg.cr / tot) * 100;
    const pOut = ((agg.out + agg.reas) / tot) * 100;
    return `
      <div class="mix-bar-wrap" title="Uncached In: ${fmtTok(agg.inp)} | Cache Write: ${fmtTok(agg.cw)} | Cache Read: ${fmtTok(agg.cr)} | Out+Reason: ${fmtTok(agg.generated)}">
        <div class="mix-track" style="width:${scalePct.toFixed(1)}%">
          <span style="width:${pIn.toFixed(1)}%;background:var(--t-uncached)"></span>
          <span style="width:${pCw.toFixed(1)}%;background:var(--t-cwrite)"></span>
          <span style="width:${pCr.toFixed(1)}%;background:var(--t-cread-bright)"></span>
          <span style="width:${pOut.toFixed(1)}%;background:var(--t-output)"></span>
        </div>
      </div>
    `;
  }

  const STRING_SORT_COLS = new Set(["name", "date", "harness", "actor", "project", "model", "title", "provider"]);

  function sortAggs(list, sortKey) {
    const st = state.sorts[sortKey];
    const col = st.col;
    const dir = st.dir;
    return list.sort((a, b) => {
      if (STRING_SORT_COLS.has(col) || typeof a[col] === "string" || typeof b[col] === "string") {
        return String(a[col] ?? "").localeCompare(String(b[col] ?? "")) * dir;
      }
      return ((Number(a[col]) || 0) - (Number(b[col]) || 0)) * dir;
    });
  }

  function thHtml(tableKey, col, label, right = true) {
    const st = state.sorts[tableKey];
    const arrow = st.col === col ? (st.dir === -1 ? " ↓" : " ↑") : "";
    const cls = ["sortable", right ? "right" : ""].filter(Boolean).join(" ");
    return `<th class="${cls}" data-sort-table="${tableKey}" data-sort-col="${col}">${label}${arrow}</th>`;
  }

  function bindSortHeaders(container) {
    container.querySelectorAll("[data-sort-table]").forEach((th) => {
      th.addEventListener("click", () => {
        const tk = th.dataset.sortTable;
        const col = th.dataset.sortCol;
        if (state.sorts[tk].col === col) {
          state.sorts[tk].dir *= -1;
        } else {
          state.sorts[tk].col = col;
          state.sorts[tk].dir = STRING_SORT_COLS.has(col) && col !== "date" ? 1 : -1;
        }
        renderAll();
      });
    });
  }

  function renderCompactDimTable(containerId, tableKey, dimName, colIdx, paletteMap, dimRows) {
    const wrap = document.getElementById(containerId);
    const map = new Map();
    for (const r of dimRows) {
      const k = r[colIdx];
      if (!map.has(k)) map.set(k, emptyAgg(k));
      addRowToAgg(map.get(k), r);
    }
    const list = sortAggs([...map.values()].map(finalizeAgg), tableKey);
    const sumPrimary = list.reduce((s, x) => s + x.primary, 0) || 1;
    const maxPrimary = Math.max(...list.map((x) => x.primary), 1);

    const rowsHtml = list
      .map((a) => {
        const col = (paletteMap && paletteMap[a.name]) || "var(--accent)";
        const sel = state.filters[dimName] === a.name;
        const share = ((a.primary / sumPrimary) * 100).toFixed(1) + "%";
        return `
          <tr class="clickable ${sel ? "selected-row" : ""}" data-row-val="${esc(a.name)}">
            <td>
              <span class="dot" style="background:${col};margin-right:7px;"></span>
              <strong>${esc(a.name)}</strong>
            </td>
            <td>${inlineMixBar(a, maxPrimary)}</td>
            <td class="right num"><strong>${fmtMetric(a.primary)}</strong></td>
            <td class="right num" style="color:var(--text-muted)">${share}</td>
            <td class="right num">${fmtTok(a.active)}</td>
            <td class="right num">${fmtTok(a.generated)}</td>
            <td class="right num">${(a.cacheHit * 100).toFixed(1)}%</td>
            <td class="right num">${fmtUsd(a.paidCost)}</td>
            <td class="right num">${fmtUsd(a.estCost)}</td>
          </tr>
        `;
      })
      .join("");

    wrap.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            ${thHtml(tableKey, "name", "Name", false)}
            <th>Volume &amp; Mix</th>
            ${thHtml(tableKey, "primary", metricColLabel(false))}
            <th class="right">Share</th>
            ${thHtml(tableKey, "active", "Fresh/Active")}
            ${thHtml(tableKey, "generated", "Out+Reason")}
            ${thHtml(tableKey, "cacheHit", "Cache%")}
            ${thHtml(tableKey, "paidCost", "Paid $")}
            ${thHtml(tableKey, "estCost", "Est. API $")}
          </tr>
        </thead>
        <tbody>${rowsHtml || '<tr><td colspan="9" style="color:var(--text-muted)">No matching records</td></tr>'}</tbody>
      </table>
    `;

    bindSortHeaders(wrap);
    wrap.querySelectorAll("[data-row-val]").forEach((tr) => {
      tr.addEventListener("click", () => toggleFilter(dimName, tr.dataset.rowVal));
    });
  }

  function renderModelTable(dimRows) {
    const wrap = document.getElementById("model-table-wrap");
    const q = state.modelSearch.trim().toLowerCase();
    const map = new Map();
    for (const r of dimRows) {
      const m = r[C_MODEL];
      if (q && !m.toLowerCase().includes(q) && !r[C_PROVIDER].toLowerCase().includes(q)) continue;
      if (!map.has(m)) map.set(m, emptyAgg(m));
      addRowToAgg(map.get(m), r);
    }
    const list = [...map.values()].map((a) => {
      finalizeAgg(a);
      a.provider = [...a.providers][0] || "Other";
      return a;
    });
    sortAggs(list, "model");
    const sumPrimary = list.reduce((s, x) => s + x.primary, 0) || 1;
    const maxPrimary = Math.max(...list.map((x) => x.primary), 1);

    const rowsHtml = list
      .map((a) => {
        const prov = a.provider;
        const pCol = PALETTE.provider[prov] || "#94a3b8";
        const harnesses = [...a.harnesses].join(", ");
        const sel = state.filters.model === a.name;
        const share = ((a.primary / sumPrimary) * 100).toFixed(1) + "%";
        return `
          <tr class="clickable ${sel ? "selected-row" : ""}" data-model="${esc(a.name)}">
            <td><strong>${esc(a.name)}</strong></td>
            <td><span class="badge"><span class="dot" style="background:${pCol}"></span>${esc(prov)}</span></td>
            <td style="color:var(--text-muted);font-size:11.5px;">${esc(harnesses)}</td>
            <td>${inlineMixBar(a, maxPrimary)}</td>
            <td class="right num"><strong>${fmtMetric(a.primary)}</strong></td>
            <td class="right num" style="color:var(--text-muted)">${share}</td>
            <td class="right num">${fmtTok(a.inp)}</td>
            <td class="right num">${fmtTok(a.cw)}</td>
            <td class="right num">${fmtTok(a.cr)}</td>
            <td class="right num">${fmtTok(a.out)}</td>
            <td class="right num">${fmtTok(a.reas)}</td>
            <td class="right num">${(a.cacheHit * 100).toFixed(1)}%</td>
            <td class="right num">${a.calls.toLocaleString()}</td>
            <td class="right num">${fmtUsd(a.paidCost)}</td>
            <td class="right num">${fmtUsd(a.estCost)}</td>
          </tr>
        `;
      })
      .join("");

    wrap.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            ${thHtml("model", "name", "Model", false)}
            ${thHtml("model", "provider", "Provider", false)}
            <th>Harnesses</th>
            <th>Mix</th>
            ${thHtml("model", "primary", metricColLabel(true))}
            <th class="right">Share</th>
            ${thHtml("model", "inp", "Uncached In")}
            ${thHtml("model", "cw", "Cache Write")}
            ${thHtml("model", "cr", "Cache Read")}
            ${thHtml("model", "out", "Output")}
            ${thHtml("model", "reas", "Reasoning")}
            ${thHtml("model", "cacheHit", "Cache%")}
            ${thHtml("model", "calls", "Calls")}
            ${thHtml("model", "paidCost", "Paid $")}
            ${thHtml("model", "estCost", "Est. API $")}
          </tr>
        </thead>
        <tbody>${rowsHtml || '<tr><td colspan="15" style="color:var(--text-muted)">No matching models</td></tr>'}</tbody>
      </table>
    `;

    bindSortHeaders(wrap);
    wrap.querySelectorAll("[data-model]").forEach((tr) => {
      tr.addEventListener("click", () => toggleFilter("model", tr.dataset.model));
    });
  }

  function renderProjectTable(dimRows) {
    const wrap = document.getElementById("project-table-wrap");
    const map = new Map();
    for (const r of dimRows) {
      const p = r[C_PROJECT];
      if (!map.has(p)) map.set(p, emptyAgg(p));
      addRowToAgg(map.get(p), r);
    }
    const list = [...map.values()].map((a) => {
      finalizeAgg(a);
      a.modelCount = a.models.size;
      return a;
    });
    sortAggs(list, "project");
    const sumPrimary = list.reduce((s, x) => s + x.primary, 0) || 1;
    const maxPrimary = Math.max(...list.map((x) => x.primary), 1);
    const pathLookup = rawData.project_paths || {};

    const rowsHtml = list
      .map((a) => {
        const sel = state.filters.project === a.name;
        const share = ((a.primary / sumPrimary) * 100).toFixed(1) + "%";
        const harnesses = [...a.harnesses].join(", ");
        const paths = pathLookup[a.name] || [];
        const pathsHtml =
          state.showPaths && paths.length
            ? `<div class="path-details">${paths.slice(0, 6).map(esc).join("<br/>")}${paths.length > 6 ? `<br/>… +${paths.length - 6} more worktrees/dirs` : ""}</div>`
            : "";
        return `
          <tr class="clickable ${sel ? "selected-row" : ""}" data-project="${esc(a.name)}">
            <td>
              <strong>${esc(a.name)}</strong>
              ${paths.length > 1 && !state.showPaths ? `<span style="color:var(--text-dim);font-size:11px;margin-left:6px;">(${paths.length} dirs)</span>` : ""}
              ${pathsHtml}
            </td>
            <td style="color:var(--text-muted);font-size:11.5px;">${esc(harnesses)}</td>
            <td>${inlineMixBar(a, maxPrimary)}</td>
            <td class="right num"><strong>${fmtMetric(a.primary)}</strong></td>
            <td class="right num" style="color:var(--text-muted)">${share}</td>
            <td class="right num">${fmtTok(a.inp)}</td>
            <td class="right num">${fmtTok(a.cw)}</td>
            <td class="right num">${fmtTok(a.cr)}</td>
            <td class="right num">${fmtTok(a.generated)}</td>
            <td class="right num">${a.modelCount}</td>
            <td class="right num">${a.calls.toLocaleString()}</td>
            <td class="right num">${fmtUsd(a.paidCost)}</td>
            <td class="right num">${fmtUsd(a.estCost)}</td>
          </tr>
        `;
      })
      .join("");

    wrap.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            ${thHtml("project", "name", "Project", false)}
            <th>Harnesses</th>
            <th>Mix</th>
            ${thHtml("project", "primary", metricColLabel(true))}
            <th class="right">Share</th>
            ${thHtml("project", "inp", "Uncached In")}
            ${thHtml("project", "cw", "Cache Write")}
            ${thHtml("project", "cr", "Cache Read")}
            ${thHtml("project", "generated", "Out+Reason")}
            ${thHtml("project", "modelCount", "Models")}
            ${thHtml("project", "calls", "Calls")}
            ${thHtml("project", "paidCost", "Paid $")}
            ${thHtml("project", "estCost", "Est. API $")}
          </tr>
        </thead>
        <tbody>${rowsHtml || '<tr><td colspan="13" style="color:var(--text-muted)">No matching projects</td></tr>'}</tbody>
      </table>
    `;

    bindSortHeaders(wrap);
    wrap.querySelectorAll("[data-project]").forEach((tr) => {
      tr.addEventListener("click", () => toggleFilter("project", tr.dataset.project));
    });
  }

  // -------------------------------------------------------------------------
  // Cross-Dimension Heatmap Matrix
  // -------------------------------------------------------------------------

  function renderMatrix(filteredRows) {
    const wrap = document.getElementById("matrix-wrap");
    let rowIdx = C_PROJECT, colIdx = C_HARNESS, maxCols = 8;
    if (state.matrixMode === "project_model") {
      rowIdx = C_PROJECT;
      colIdx = C_MODEL;
      maxCols = 8;
    } else if (state.matrixMode === "harness_model") {
      rowIdx = C_HARNESS;
      colIdx = C_MODEL;
      maxCols = 8;
    }

    const rowTotals = new Map();
    const colTotals = new Map();
    const cells = new Map();

    for (const r of filteredRows) {
      const rk = r[rowIdx];
      const ck = r[colIdx];
      const v = metricFromRow(r);
      if (v <= 0) continue;
      rowTotals.set(rk, (rowTotals.get(rk) || 0) + v);
      colTotals.set(ck, (colTotals.get(ck) || 0) + v);
      const key = rk + "\0" + ck;
      cells.set(key, (cells.get(key) || 0) + v);
    }

    const rows = [...rowTotals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12).map((e) => e[0]);
    const cols = [...colTotals.entries()].sort((a, b) => b[1] - a[1]).slice(0, maxCols).map((e) => e[0]);

    let maxCell = 1e-9;
    for (const rk of rows) {
      for (const ck of cols) {
        const v = cells.get(rk + "\0" + ck) || 0;
        if (v > maxCell) maxCell = v;
      }
    }

    const headCols = cols.map((c) => `<th class="right">${esc(c)}</th>`).join("");
    const bodyRows = rows
      .map((rk) => {
        const tds = cols
          .map((ck) => {
            const v = cells.get(rk + "\0" + ck) || 0;
            if (v <= 0) return `<td class="right num" style="color:var(--text-dim)">—</td>`;
            const alpha = (0.08 + 0.45 * Math.sqrt(v / maxCell)).toFixed(2);
            return `<td class="right"><div class="matrix-cell" style="background:rgba(92,200,255,${alpha})">${fmtMetric(v)}</div></td>`;
          })
          .join("");
        return `
          <tr>
            <td><strong>${esc(rk)}</strong></td>
            ${tds}
            <td class="right num"><strong>${fmtMetric(rowTotals.get(rk) || 0)}</strong></td>
          </tr>
        `;
      })
      .join("");

    wrap.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>${rowIdx === C_PROJECT ? "Project" : "Harness"} \\ ${colIdx === C_HARNESS ? "Harness" : "Model"}</th>
            ${headCols}
            <th class="right">Row Total</th>
          </tr>
        </thead>
        <tbody>${bodyRows || '<tr><td colspan="10" style="color:var(--text-muted)">No matrix data</td></tr>'}</tbody>
      </table>
    `;
  }

  // -------------------------------------------------------------------------
  // Heavyweight Sessions Explorer
  // -------------------------------------------------------------------------

  function renderSessions(preMatchedSessions) {
    const wrap = document.getElementById("sessions-table-wrap");
    const sub = document.getElementById("sessions-subtitle");
    const q = state.sessionSearch.trim().toLowerCase();

    const baseList = preMatchedSessions || (rawData.top_sessions || []).filter(sessionMatches);
    const matched = baseList
      .filter((s) => {
        if (!q) return true;
        const hay = `${s.title} ${s.project} ${s.model} ${s.harness} ${s.id}`.toLowerCase();
        return hay.includes(q);
      })
      .map((s) => {
        const inp = s.input_uncached || 0;
        const cr = s.cache_read || 0;
        const cw = s.cache_write || 0;
        const out = s.output_text || 0;
        const reas = s.reasoning || 0;
        const estCost = calcEstCost(s.model, inp, cr, cw, out, reas);
        const total = inp + cr + cw + out + reas;
        const active = inp + cw + out + reas;
        const generated = out + reas;
        return { ...s, inp, cr, cw, out, reas, total, active, generated, estCost };
      });

    sortAggs(matched, "sessions");
    const shown = matched.slice(0, 60);
    sub.textContent = `Showing top ${shown.length.toLocaleString()} of ${matched.length.toLocaleString()} sessions matching active filters`;

    const rowsHtml = shown
      .map((s) => {
        const hCol = PALETTE.harness[s.harness] || "var(--accent)";
        const aCol = PALETTE.actor[s.actor] || "#94a3b8";
        return `
          <tr>
            <td class="num" style="white-space:nowrap;color:var(--text-muted)">${esc(s.date)}</td>
            <td><span class="badge"><span class="dot" style="background:${hCol}"></span>${esc(s.harness)}</span></td>
            <td><span class="badge"><span class="dot" style="background:${aCol}"></span>${esc(s.actor)}</span></td>
            <td><strong>${esc(s.project)}</strong></td>
            <td class="mono" style="font-size:11.5px">${esc(s.model)}</td>
            <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(s.title)}">${esc(s.title)}</td>
            <td class="right num">${s.calls.toLocaleString()}</td>
            <td class="right num">${fmtTok(s.active)}</td>
            <td class="right num">${fmtTok(s.cr)}</td>
            <td class="right num">${fmtTok(s.generated)}</td>
            <td class="right num"><strong>${fmtTok(s.total)}</strong></td>
            <td class="right num">${fmtUsd(s.estCost)}</td>
          </tr>
        `;
      })
      .join("");

    wrap.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            ${thHtml("sessions", "date", "Date", false)}
            ${thHtml("sessions", "harness", "Harness", false)}
            ${thHtml("sessions", "actor", "Role", false)}
            ${thHtml("sessions", "project", "Project", false)}
            ${thHtml("sessions", "model", "Model", false)}
            ${thHtml("sessions", "title", "Title / Initial Prompt", false)}
            ${thHtml("sessions", "calls", "Calls")}
            ${thHtml("sessions", "active", "Fresh/Active")}
            ${thHtml("sessions", "cr", "Cache Read")}
            ${thHtml("sessions", "generated", "Out+Reason")}
            ${thHtml("sessions", "total", "Total")}
            ${thHtml("sessions", "estCost", "Est. $")}
          </tr>
        </thead>
        <tbody>${rowsHtml || '<tr><td colspan="12" style="color:var(--text-muted)">No matching sessions</td></tr>'}</tbody>
      </table>
    `;
    bindSortHeaders(wrap);
  }

  // -------------------------------------------------------------------------
  // Health Cards & Rate Modal
  // -------------------------------------------------------------------------

  function renderHealth() {
    const grid = document.getElementById("health-grid");
    grid.innerHTML = (rawData.source_health || [])
      .map((h) => {
        const col = PALETTE.harness[h.harness] || "var(--accent)";
        return `
          <div class="health-card">
            <div class="health-head">
              <span><span class="dot" style="background:${col};margin-right:6px;"></span>${esc(h.harness)}</span>
              <span class="num" style="color:var(--text-muted)">${h.elapsed_ms} ms</span>
            </div>
            <div class="mono" style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">${esc(h.source_path)}</div>
            <div style="color:var(--text-muted)">
              <strong>${(h.sessions_count || 0).toLocaleString()}</strong> runs ·
              <strong>${(h.api_calls || 0).toLocaleString()}</strong> turns ·
              ${h.files_total} sources (${h.cache_hits} cached)
            </div>
            <div style="color:var(--text-dim);font-size:11px;margin-top:2px;">
              Active span: <span class="mono">${esc(h.live_date_range)}</span>
              ${h.archived_days_recovered ? ` · +${h.archived_days_recovered} pre-cleanup days` : ""}
            </div>
          </div>
        `;
      })
      .join("");
  }

  function openRatesModal() {
    const modal = document.getElementById("rates-modal");
    const wrap = document.getElementById("rates-table-wrap");
    const defs = rawData.default_rates_per_1m || {};
    const models = Object.keys(defs).sort();

    wrap.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Model</th>
            <th class="right">Uncached In ($/1M)</th>
            <th class="right">Cache Write ($/1M)</th>
            <th class="right">Cache Read ($/1M)</th>
            <th class="right">Output+Reason ($/1M)</th>
            <th>Basis / Note</th>
          </tr>
        </thead>
        <tbody>
          ${models
            .map((m) => {
              const r = getModelRate(m);
              return `
                <tr data-rate-model="${esc(m)}">
                  <td class="mono"><strong>${esc(m)}</strong></td>
                  <td class="right"><input class="rate-input" type="number" step="0.05" min="0" data-rk="in" value="${r.in ?? 0}" /></td>
                  <td class="right"><input class="rate-input" type="number" step="0.05" min="0" data-rk="cw" value="${r.cw ?? 0}" /></td>
                  <td class="right"><input class="rate-input" type="number" step="0.01" min="0" data-rk="cr" value="${r.cr ?? 0}" /></td>
                  <td class="right"><input class="rate-input" type="number" step="0.1" min="0" data-rk="out" value="${r.out ?? 0}" /></td>
                  <td style="color:var(--text-dim);font-size:11.5px;">${esc(r.note || "")}</td>
                </tr>
              `;
            })
            .join("")}
        </tbody>
      </table>
    `;
    modal.hidden = false;
  }

  // -------------------------------------------------------------------------
  // Editable Out-of-Pocket Billing & Overage Ledger Modal
  // -------------------------------------------------------------------------

  function srcBadgeLabel(src) {
    if (src === "extracted") return "Extracted";
    if (src === "inferred") return "Inferred";
    if (src === "user_calibrated") return "Calibrated";
    return "Custom";
  }

  function catLabel(cat) {
    if (cat === "subscription") return "Subscription";
    if (cat === "overage") return "Overage / Credits";
    if (cat === "promo") return "Free Promo ($0)";
    return cat || "Other";
  }

  function collectBillingModalRows() {
    const baseById = new Map(getEffectiveLedger().map((r) => [r.id, r]));
    const rows = [];
    document.querySelectorAll("#billing-ledger-table-wrap tr[data-bill-id]").forEach((tr) => {
      const id = tr.dataset.billId;
      const isCustom = tr.dataset.billCustom === "1";
      const base = baseById.get(id) || {};
      const getVal = (k) => {
        const el = tr.querySelector(`[data-bk="${k}"]`);
        if (!el) return base[k];
        if (el.type === "checkbox") return el.checked;
        return el.value;
      };
      const amt = Math.max(0, parseFloat(getVal("amount_usd")) || 0);
      rows.push({
        ...base,
        id,
        is_custom: isCustom,
        enabled: Boolean(getVal("enabled")),
        harness: String(getVal("harness") || base.harness || "Claude Code"),
        category: String(getVal("category") || base.category || "overage"),
        label: String(getVal("label") ?? base.label ?? ""),
        start_date: String(getVal("start_date") || base.start_date || ""),
        end_date: String(getVal("end_date") || base.end_date || ""),
        amount_usd: amt,
        promo_value_usd: Number(base.promo_value_usd || 0),
        source: isCustom ? "custom" : base.source || "extracted",
        detail: base.detail || (isCustom ? "Custom user-added billing entry" : ""),
      });
    });
    return rows;
  }

  function updateBillingModalLiveTotals() {
    const sumEl = document.getElementById("billing-modal-live-summary");
    if (!sumEl) return;
    const rows = collectBillingModalRows();
    let sub = 0, ov = 0, promo = 0;
    for (const r of rows) {
      if (!r.enabled) continue;
      if (r.category === "subscription") sub += r.amount_usd;
      else if (r.category === "overage") ov += r.amount_usd;
      promo += Number(r.promo_value_usd || 0);
    }
    sumEl.innerHTML = `
      <span>Subscriptions: <strong class="num" style="color:var(--text)">${fmtUsd(sub)}</strong></span>
      <span>Overages / Credits: <strong class="num" style="color:var(--text)">${fmtUsd(ov)}</strong></span>
      <span>Promo Absorbed ($0 paid): <strong class="num" style="color:#34d399">${fmtUsd(promo)}</strong></span>
      <span>Total Actual Out-of-Pocket: <strong class="num" style="color:var(--accent);font-size:13.5px;">${fmtUsd(sub + ov)}</strong></span>
    `;
  }

  function openBillingModal(draftRows = null) {
    const modal = document.getElementById("billing-modal");
    const wrap = document.getElementById("billing-ledger-table-wrap");
    if (!modal || !wrap) return;

    const rows = Array.isArray(draftRows) ? draftRows : getEffectiveLedger();
    const harnesses = ["Claude Code", "Codex", "OpenCode", "Antigravity"];

    wrap.innerHTML = `
      <div id="billing-modal-live-summary" style="display:flex;flex-wrap:wrap;gap:18px;padding:9px 12px;margin-bottom:10px;border-radius:7px;background:var(--bg-subtle);border:1px solid var(--border);font-size:12.5px;color:var(--text-muted);"></div>
      <table class="data-table">
        <thead>
          <tr>
            <th>On</th>
            <th>Harness</th>
            <th>Category</th>
            <th>Plan / Overage Line Item</th>
            <th>Start</th>
            <th>End</th>
            <th class="right">Paid ($)</th>
            <th>Provenance</th>
            <th>Local Log Evidence / Notes</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${rows
            .map((r) => {
              const col = PALETTE.harness[r.harness] || "var(--accent)";
              const hCell = r.is_custom
                ? `<select data-bk="harness">
                    ${harnesses
                      .map((h) => `<option value="${esc(h)}" ${r.harness === h ? "selected" : ""}>${esc(h)}</option>`)
                      .join("")}
                   </select>`
                : `<span class="dot" style="background:${col};margin-right:6px;"></span><strong>${esc(r.harness)}</strong>`;
              const cCell = r.is_custom
                ? `<select data-bk="category">
                    <option value="overage" ${r.category === "overage" ? "selected" : ""}>Overage / Credits</option>
                    <option value="subscription" ${r.category === "subscription" ? "selected" : ""}>Subscription</option>
                   </select>`
                : `<span style="color:var(--text-muted);font-size:12px;">${esc(catLabel(r.category))}</span>`;
              const srcCls = "src-" + String(r.source || "custom").replace(/[^a-z0-9_]/gi, "");
              return `
                <tr data-bill-id="${esc(r.id)}" data-bill-custom="${r.is_custom ? "1" : "0"}">
                  <td><input type="checkbox" data-bk="enabled" ${r.enabled !== false ? "checked" : ""} /></td>
                  <td style="white-space:nowrap;">${hCell}</td>
                  <td style="white-space:nowrap;">${cCell}</td>
                  <td><input class="bill-input-text" type="text" data-bk="label" value="${esc(r.label)}" /></td>
                  <td><input type="date" data-bk="start_date" value="${esc(r.start_date || "")}" /></td>
                  <td><input type="date" data-bk="end_date" value="${esc(r.end_date || "")}" /></td>
                  <td class="right"><input class="bill-input-usd" type="number" step="0.01" min="0" data-bk="amount_usd" value="${Number(r.amount_usd || 0).toFixed(2)}" /></td>
                  <td><span class="src-pill ${srcCls}">${esc(srcBadgeLabel(r.source))}</span></td>
                  <td style="color:var(--text-dim);font-size:11.5px;max-width:290px;white-space:normal;">${esc(r.detail || "")}</td>
                  <td>${r.is_custom ? `<button type="button" data-del-bill="${esc(r.id)}" title="Remove custom entry" style="padding:3px 7px;">✕</button>` : ""}</td>
                </tr>
              `;
            })
            .join("")}
        </tbody>
      </table>
    `;

    wrap.querySelectorAll("input, select").forEach((el) => {
      el.addEventListener("input", updateBillingModalLiveTotals);
      el.addEventListener("change", updateBillingModalLiveTotals);
    });
    wrap.querySelectorAll("[data-del-bill]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const nextRows = collectBillingModalRows().filter((r) => r.id !== btn.dataset.delBill);
        openBillingModal(nextRows);
      });
    });

    updateBillingModalLiveTotals();
    modal.hidden = false;
  }

  async function persistBillingOverrides(nextOv) {
    const hasItems = Object.keys(nextOv.items || {}).length > 0;
    const hasCustom = (nextOv.custom_items || []).length > 0;
    const clean = hasItems || hasCustom ? nextOv : { items: {}, custom_items: [] };
    billingOverrides = clean;
    if (rawData && rawData.billing) {
      rawData.billing.saved_overrides = hasItems || hasCustom ? clean : {};
    }
    try {
      if (hasItems || hasCustom) {
        localStorage.setItem(STORAGE_BILLING_KEY, JSON.stringify(clean));
      } else {
        localStorage.removeItem(STORAGE_BILLING_KEY);
      }
    } catch {
      // ignore storage quota errors
    }
    if (window.location.protocol !== "file:") {
      try {
        await fetch("/api/billing", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(hasItems || hasCustom ? clean : {}),
        });
      } catch {
        // offline / static server fallback already saved in localStorage
      }
    }
  }

  function showToast(msg) {
    const t = document.getElementById("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => {
      t.hidden = true;
    }, 3600);
  }

  // -------------------------------------------------------------------------
  // CSV Export & Live Refresh
  // -------------------------------------------------------------------------

  function csvCell(v) {
    const s = String(v ?? "");
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }

  function exportFilteredCsv() {
    const rows = rawData.cube.filter((r) => rowMatches(r));
    const header = [
      "date", "harness", "provider", "model", "project", "actor", "archived",
      "calls", "sessions", "input_uncached", "cache_read", "cache_write",
      "output_text", "reasoning", "total_tokens", "logged_cost_usd",
      "paid_attributed_usd", "promo_absorbed_usd", "est_api_cost_usd",
    ];
    const lines = [header.join(",")];
    for (const r of rows) {
      const tot = r[C_IN] + r[C_CR] + r[C_CW] + r[C_OUT] + r[C_REAS];
      const est = calcEstCost(r[C_MODEL], r[C_IN], r[C_CR], r[C_CW], r[C_OUT], r[C_REAS]);
      const rb = getRowBilling(r);
      const vals = [
        r[C_DATE], r[C_HARNESS], r[C_PROVIDER], r[C_MODEL], r[C_PROJECT], r[C_ACTOR], r[C_ARCHIVED],
        r[C_CALLS], r[C_SESSIONS], r[C_IN], r[C_CR], r[C_CW], r[C_OUT], r[C_REAS], tot,
        r[C_COST].toFixed(6), rb.paid.toFixed(6), rb.promo.toFixed(6), est.toFixed(6),
      ].map(csvCell);
      lines.push(vals.join(","));
    }
    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `token-observatory-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
    showToast(`Exported ${rows.length.toLocaleString()} daily cube rows to CSV.`);
  }

  async function refreshFromServer() {
    if (window.location.protocol === "file:") {
      showToast("Opened via file:// — run `token-stats` (or `token-stats extract`) to refresh data.");
      return;
    }
    const btn = document.getElementById("btn-refresh");
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = "↻ Scanning…";
    try {
      const resp = await fetch("/api/refresh", { method: "POST" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      rawData = await resp.json();
      onDataLoaded();
      billingOverrides = loadSavedBillingOverrides();
      renderMonthPresets();
      if (state.rangePreset !== "custom") applyRangePreset(state.rangePreset);
      renderAll();
      showToast(`Refreshed local logs in ${rawData.extraction_ms} ms.`);
    } catch (err) {
      showToast("Refresh requires `token-stats` server running (" + err.message + ")");
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  }

  // -------------------------------------------------------------------------
  // Master Render Loop & Initialization
  // -------------------------------------------------------------------------

  function renderAll() {
    if (!rawData || !rawData.cube) return;
    recomputeBillingAllocations();
    const filteredRows = rawData.cube.filter((r) => rowMatches(r));
    const filteredHourly = hourlyRows.filter((r) => rowMatches(r));
    const grand = emptyAgg("grand");
    for (const r of filteredRows) addRowToAgg(grand, r);
    finalizeAgg(grand);

    const matchedSessions = (rawData.top_sessions || []).filter(sessionMatches);

    renderHeaderAndChips(matchedSessions.length);
    renderKPIs(grand, matchedSessions.length);
    renderBillingPanel();
    renderTimeline(filteredRows, filteredHourly);
    renderActivityCalendar(filteredRows);
    renderAnatomyAndRoles(grand, filteredRows, rawData.cube.filter((r) => rowMatches(r, "actor")), filteredHourly);
    renderCompactDimTable(
      "harness-table-wrap",
      "harness",
      "harness",
      C_HARNESS,
      PALETTE.harness,
      rawData.cube.filter((r) => rowMatches(r, "harness"))
    );
    renderCompactDimTable(
      "provider-table-wrap",
      "provider",
      "provider",
      C_PROVIDER,
      PALETTE.provider,
      rawData.cube.filter((r) => rowMatches(r, "provider"))
    );
    renderModelTable(rawData.cube.filter((r) => rowMatches(r, "model")));
    renderProjectTable(rawData.cube.filter((r) => rowMatches(r, "project")));
    renderMatrix(filteredRows);
    renderSessions(matchedSessions);
    renderHealth();
  }

  function init() {
    const savedTheme = localStorage.getItem(STORAGE_THEME_KEY) || "dark";
    document.documentElement.setAttribute("data-theme", savedTheme);
    applyThemePalette(savedTheme);

    document.getElementById("btn-theme").addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme");
      const next = cur === "light" ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", next);
      applyThemePalette(next);
      localStorage.setItem(STORAGE_THEME_KEY, next);
      renderAll();
    });

    document.querySelectorAll("#range-presets > .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        applyRangePreset(btn.dataset.range);
        renderAll();
      });
    });

    for (const id of ["date-from", "date-to"]) {
      document.getElementById(id).addEventListener("change", () => {
        let df = document.getElementById("date-from").value;
        let dt = document.getElementById("date-to").value;
        if (!df && !dt) {
          applyRangePreset("all");
          renderAll();
          return;
        }
        if (df && dt && df > dt) {
          const tmp = df;
          df = dt;
          dt = tmp;
          document.getElementById("date-from").value = df;
          document.getElementById("date-to").value = dt;
        }
        state.dateFrom = df;
        state.dateTo = dt;
        state.rangePreset = "custom";
        document.querySelectorAll("#range-presets .seg-btn").forEach((b) => b.classList.remove("active"));
        renderAll();
      });
    }

    document.getElementById("metric-select").addEventListener("change", (e) => {
      state.metric = e.target.value;
      renderAll();
    });

    document.getElementById("chk-archived").addEventListener("change", (e) => {
      state.includeArchived = e.target.checked;
      renderMonthPresets();
      if (state.rangePreset !== "custom") applyRangePreset(state.rangePreset);
      renderAll();
    });

    document.getElementById("chk-show-paths").addEventListener("change", (e) => {
      state.showPaths = e.target.checked;
      renderAll();
    });

    document.querySelectorAll("#chart-stack-group .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.chartStack = btn.dataset.stack;
        document.querySelectorAll("#chart-stack-group .seg-btn").forEach((b) =>
          b.classList.toggle("active", b === btn)
        );
        renderAll();
      });
    });

    document.querySelectorAll("#chart-grain-group .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.chartGrain = btn.dataset.grain;
        document.querySelectorAll("#chart-grain-group .seg-btn").forEach((b) =>
          b.classList.toggle("active", b === btn)
        );
        renderAll();
      });
    });

    document.querySelectorAll("#matrix-mode-group .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.matrixMode = btn.dataset.matrix;
        document.querySelectorAll("#matrix-mode-group .seg-btn").forEach((b) =>
          b.classList.toggle("active", b === btn)
        );
        renderAll();
      });
    });

    document.getElementById("model-search").addEventListener("input", (e) => {
      state.modelSearch = e.target.value;
      renderAll();
    });

    document.getElementById("session-search").addEventListener("input", (e) => {
      state.sessionSearch = e.target.value;
      renderSessions();
    });

    document.getElementById("btn-export-csv").addEventListener("click", exportFilteredCsv);
    document.getElementById("btn-refresh").addEventListener("click", refreshFromServer);

    // Billing & Overages modal events
    document.getElementById("btn-billing").addEventListener("click", () => openBillingModal());
    document.getElementById("btn-open-billing-inline").addEventListener("click", () => openBillingModal());
    document.getElementById("btn-close-billing").addEventListener("click", () => {
      document.getElementById("billing-modal").hidden = true;
    });
    document.getElementById("billing-modal").addEventListener("click", (e) => {
      if (e.target.id === "billing-modal") e.target.hidden = true;
    });
    document.getElementById("btn-add-billing-item").addEventListener("click", () => {
      const curRows = collectBillingModalRows();
      const today = new Date().toISOString().slice(0, 10);
      curRows.push({
        id: "custom_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 5),
        harness: "Claude Code",
        category: "overage",
        label: "Additional Extra Usage / Plan Entry",
        start_date: state.dateFrom || today.slice(0, 8) + "01",
        end_date: state.dateTo || today,
        amount_usd: 50,
        promo_value_usd: 0,
        source: "custom",
        detail: "Custom user-added billing entry",
        enabled: true,
        is_custom: true,
      });
      openBillingModal(curRows);
    });
    document.getElementById("btn-save-billing").addEventListener("click", async () => {
      const defsById = new Map(((rawData && rawData.billing && rawData.billing.default_ledger) || []).map((d) => [d.id, d]));
      const items = {};
      const custom_items = [];
      for (const r of collectBillingModalRows()) {
        if (r.is_custom) {
          custom_items.push({
            id: r.id,
            enabled: r.enabled,
            harness: r.harness,
            category: r.category,
            label: r.label,
            start_date: r.start_date,
            end_date: r.end_date,
            amount_usd: r.amount_usd,
            detail: r.detail || "Custom user-added billing entry",
          });
        } else {
          const def = defsById.get(r.id);
          if (!def) continue;
          const diff = {};
          if (r.enabled !== true) diff.enabled = false;
          if (r.label !== def.label) diff.label = r.label;
          if (r.start_date !== def.start_date) diff.start_date = r.start_date;
          if (r.end_date !== def.end_date) diff.end_date = r.end_date;
          if (Math.abs(r.amount_usd - Number(def.amount_usd || 0)) > 1e-4) diff.amount_usd = r.amount_usd;
          if (Object.keys(diff).length > 0) items[r.id] = diff;
        }
      }
      await persistBillingOverrides({ items, custom_items });
      document.getElementById("billing-modal").hidden = true;
      renderAll();
      showToast("Saved out-of-pocket billing & overage ledger.");
    });
    document.getElementById("btn-reset-billing").addEventListener("click", async () => {
      await persistBillingOverrides({ items: {}, custom_items: [] });
      openBillingModal();
      renderAll();
      showToast("Reset billing ledger to log-extracted & calibrated defaults.");
    });

    // Rates modal events — only persist user-modified overrides so future defaults stay fresh
    document.getElementById("btn-rates").addEventListener("click", openRatesModal);
    document.getElementById("btn-close-rates").addEventListener("click", () => {
      document.getElementById("rates-modal").hidden = true;
    });
    document.getElementById("rates-modal").addEventListener("click", (e) => {
      if (e.target.id === "rates-modal") e.target.hidden = true;
    });
    document.getElementById("btn-save-rates").addEventListener("click", () => {
      const next = { ...customRates };
      document.querySelectorAll("#rates-table-wrap tr[data-rate-model]").forEach((tr) => {
        const m = tr.dataset.rateModel;
        const def = getDefaultModelRate(m);
        const rec = { note: "Custom override" };
        tr.querySelectorAll("input[data-rk]").forEach((inp) => {
          rec[inp.dataset.rk] = Math.max(0, parseFloat(inp.value) || 0);
        });
        const changed = ["in", "cw", "cr", "out"].some(
          (k) => Math.abs((rec[k] ?? 0) - (def[k] ?? 0)) > 1e-6
        );
        if (changed) {
          next[m] = rec;
        } else {
          delete next[m];
        }
      });
      customRates = next;
      if (Object.keys(customRates).length > 0) {
        localStorage.setItem(STORAGE_RATES_KEY, JSON.stringify(customRates));
      } else {
        localStorage.removeItem(STORAGE_RATES_KEY);
      }
      document.getElementById("rates-modal").hidden = true;
      renderAll();
      showToast("Updated API-equivalent rate card.");
    });
    document.getElementById("btn-reset-rates").addEventListener("click", () => {
      customRates = {};
      localStorage.removeItem(STORAGE_RATES_KEY);
      openRatesModal();
      renderAll();
      showToast("Reset rate card to calibrated defaults.");
    });

    let resizeRaf = 0;
    window.addEventListener("resize", () => {
      cancelAnimationFrame(resizeRaf);
      resizeRaf = requestAnimationFrame(() => {
        if (!rawData) return;
        const rows = rawData.cube.filter((r) => rowMatches(r));
        renderTimeline(rows, hourlyRows.filter((r) => rowMatches(r)));
        renderActivityCalendar(rows);
      });
    });

    if (rawData) {
      onDataLoaded();
      billingOverrides = loadSavedBillingOverrides();
      renderMonthPresets();
      applyRangePreset("all");
      renderAll();
    } else if (window.location.protocol !== "file:") {
      fetch("/api/stats")
        .then((r) => r.json())
        .then((d) => {
          rawData = d;
          onDataLoaded();
          billingOverrides = loadSavedBillingOverrides();
          renderMonthPresets();
          applyRangePreset("all");
          renderAll();
        });
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
