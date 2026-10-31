# Local AI Token Observatory (`token-stats`)

Zero-dependency Python + Vanilla JS dashboard that extracts, normalizes, caches, and visualizes AI coding agent token usage and **actual out-of-pocket spend** from local harness logs.

- **Zero PyPI packages** — 100% Python 3.14+ standard library (`sqlite3`, `compression.zstd`, `concurrent.futures`, `http.server`).
- **Zero npm packages / CDNs** — 100% plain HTML, CSS, Vanilla JS, and responsive inline SVG charts. Works completely offline (both via `http://127.0.0.1:8765` and directly via `file://`).
- **Fast & Repeatable** — Uses a local SQLite incremental `(path, mtime_ns, size)` parse cache (`data/parse_cache.sqlite`) across CPU cores (~1–2s warm incremental refresh).

## Installation & Quick Start

`./install.sh` installs the `token-stats` launcher into `~/.local/bin/token-stats` (standard XDG user binary directory):

```bash
./install.sh
```

Once installed, run from anywhere:

```bash
# 1. Default: incrementally refresh logs, print terminal summary & serve http://127.0.0.1:8765
token-stats

# 2. Start server (or reuse an already-running instance) and open the browser tab
token-stats --open
token-stats --port 9000

# 3. Print terminal breakdown tables only (without starting the HTTP server)
token-stats summary

# 4. Force a clean full re-parse of all raw logs from scratch
token-stats extract --rebuild
```

You can also open `web/index.html` directly via `file://` in any browser without running the server (`web/data.js` is regenerated every time `token-stats` runs, and git-ignored along with `data/`).

## Supported Local Telemetry Sources

| Harness | Local Paths Read (Read-Only) | Key Notes |
| --- | --- | --- |
| **Claude Code** | `~/.claude/projects/**/*.jsonl` + `~/.claude/stats-cache.json` + `~/.claude.json` (non-sensitive billing metadata) | Deduplicates streaming chunks globally by `message.id` (preferring completed `stop_reason` chunks and splitting server-side `fallback_message` iterations); separates subagents (`subagents/*.jsonl` / `isSidechain`); recovers pre-cleanup history from `stats-cache.json`; extracts subscription start date, cached Extra Usage snapshots, and weekly quota step-ups. |
| **Codex** | `~/.codex/state_5.sqlite` + `~/.codex/sessions/**/*.jsonl[.zst]` + `archived_sessions/` | Decompresses `.jsonl.zst` via Python 3.14 `compression.zstd`, strips inherited parent prefix/compaction turns from forked subagents & `guardian` auto-reviews, recovers pre-restart tokens, and reconstructs exact `rate_limits.plan_type` cycles and `credits.balance` wallet purchases & turn-by-turn credit burns (`$0.04/credit`). |
| **OpenCode** | `~/.local/share/opencode/opencode.db` | Streaming-safe per-session signature sync across `session_v2`, `message` (v1), and `session_message` (v2 + v1->v2 hybrid continuations), tracking direct API spend, promotional credit offsets, and local self-hosted GPU models (`$0`). |
| **Antigravity** | `~/.gemini/antigravity/conversations/*.db` + `antigravity-ide/conversations/*.db` + `conversation_summaries.db` | Decodes `gen_metadata` & `steps` protobuf blobs directly from trajectory SQLite DBs (WAL-aware), recovering exact per-generation API model slugs, historical turns, and Gemini `thinking_tokens`. |

## Actual Out-of-Pocket Billing & Overage Ledger

In addition to list-rate API-equivalent valuation, the observatory builds an **Actual Out-of-Pocket Ledger** (`payload["billing"]` + `💳 Billing & Overages` in the UI):

- **Codex (`Extracted Exact`)**: Reconstructs `plan_type` billing cycles (`plus`, `prolite`, `pro`, `team`) and exact `500`-credit (`$20`) wallet purchases + turn-level burns from non-forked `rate_limits.credits.balance` telemetry.
- **Claude Code (`Extracted + Inferred + Editable`)**: Combines `subscriptionCreatedAt` and `cachedUsageUtilization` from `~/.claude.json` with weekly API-equivalent quota step-ups (`Max 5x` → `Max 20x`) and burst overage weighting. Any line item (such as Stripe Extra Usage totals) can be customized in the UI and persists locally to git-ignored `data/billing_overrides.json` (`POST /api/billing`) and `localStorage`.
- **OpenCode & Antigravity**: Supports logged direct API spend, promotional credit offsets (`$0` paid), local GPU (`$0`), and custom monthly plan schedules via the UI or optional git-ignored `data/local_config.json`.

## Harmonized Token Accounting

Different providers report token fields differently (e.g., Codex `input_tokens` includes `cached_input_tokens`, and Codex/Claude `output_tokens` includes `reasoning`/`thinking` tokens, whereas OpenCode and Antigravity report `input` as uncached-only). Every turn is normalized into **5 mutually exclusive, additive buckets**:

1. `input_uncached` — Fresh prompt tokens
2. `cache_write` — Newly created prompt cache tokens
3. `cache_read` — Reused cached prompt tokens
4. `output_text` — Visible non-reasoning completion tokens
5. `reasoning` — Thinking / reasoning completion tokens

## Time Views

- **Token Velocity Over Time** stacks the top 10 series (harness, provider, project, model, token type or role) plus an `Other` bucket, at **Hourly**, **Daily** or **Weekly** grain. Hourly buckets use local time and show up to the latest 31 days of the selected range; pre-cleanup Claude history (`stats-cache.json`) has no hour-level timestamps and is left out of hourly views. Hover a legend item to spotlight that series on the baseline; click it to filter.
- **Daily Activity Calendar** is a GitHub-style day grid shaded by quartile of the active Primary Metric, with active-day and streak stats. Click a day to isolate it.
- **Hour-of-Day Cadence** sits next to the day-of-week profile.
- **Range selection:** the Range bar has `1d`, `3d`, `7d`, `14d`, `30d` and month presets (counted back from the latest day with data). On the velocity chart and the calendar, click a bar/day to select it, or press and drag across bars/days to select a span; everything on the page filters to that range. In the hourly view a drag selects exact hours (e.g. `2026-10-29 15:00 → 2026-10-30 10:59`), shown as a ⏱ pill next to the date pickers; its ✕ widens back to whole days, and clicking an hourly bar opens its whole day. Session counts stay day-level, since sessions only carry a date.

The payload carries a compact `cube_hourly` table alongside the daily `cube`: each row is `[cube_row, hour, calls, input_uncached, cache_read, cache_write, output_text, reasoning, logged_cost_usd]`, where `cube_row` indexes the daily row that holds its dimensions.

## Session Timelines

Every session in **Heavyweight Sessions & Subagent Runs** links to `session.html`, a call-by-call view of that session together with its subagents (and Codex forks / auto-reviews):

- **Token Burn Over Time** — stacked by agent, token type or model, per interval or cumulative.
- **Agent Lanes** — when the main session and each subagent were active, with dashed connectors where a subagent was spawned.
- **Context Size per Call** — prompt size on every call per agent, with ▼ marking sharp drops (usually compactions).
- **Agents** table — calls, tokens, est. $, peak context and drops per agent for the visible window.

Drag across any chart to zoom all of them to a time window; click a lane, legend entry or table row to focus one agent. Each session group is written to git-ignored `web/sessions/<id>.js` during extraction (unchanged files are not rewritten), so the page also works offline via `file://`.
