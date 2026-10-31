// Shared helpers for the observatory pages (index.html and session.html). Plain script, no modules,
// so both pages keep working when opened via file://.
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

  const STORAGE_RATES_KEY = "token_obs_rates_v1";
  const STORAGE_THEME_KEY = "token_obs_theme_v1";

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

  function esc(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function loadSavedRates() {
    try {
      const raw = localStorage.getItem(STORAGE_RATES_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch {
      return {};
    }
  }

  function savedTheme() {
    try {
      return localStorage.getItem(STORAGE_THEME_KEY) || "dark";
    } catch {
      return "dark";
    }
  }

  function saveTheme(theme) {
    try {
      localStorage.setItem(STORAGE_THEME_KEY, theme);
    } catch {}
  }

  window.TokenObs = {
    PALETTES,
    STORAGE_RATES_KEY,
    STORAGE_THEME_KEY,
    fmtTok,
    fmtUsd,
    esc,
    loadSavedRates,
    savedTheme,
    saveTheme,
  };
})();
