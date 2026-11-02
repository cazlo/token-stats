# AGENTS.md

Guidance for AI coding agents working in this repo. See `README.md` for what the tool does.

## Layout

- `token_stats.py` — the whole backend: per-harness extractors (Claude Code, Codex, OpenCode, Antigravity, GitHub Copilot), merge into the `cube` / `cube_hourly` payload, billing ledger, CLI and local HTTP server.
- `web/` — `index.html` + `app.js` (the dashboard), `session.html` + `session.js` + `session.css` (per-session timeline page), shared `common.js` (palettes, formatting, storage keys) and `styles.css`. Vanilla JS, inline SVG charts.
- Generated, git-ignored: `data/` (parse cache, `stats.json`, billing overrides, session-detail manifest), `web/data.js` and `web/sessions/*.js` (one per-call timeline per session group). Never commit these — they hold the user's local usage data.

## Hard constraints

- **Zero dependencies.** Python 3.14+ standard library only (it uses `compression.zstd`); no PyPI packages. No npm packages, CDNs or build step in `web/` — the page must work offline and via `file://`.
- **Read-only on sources.** Extractors only read harness logs and databases under `~`; never write to them.
- Keep the server bound to localhost by default.

## Working rules

- If you change what a worker parses or what gets stored in the parse cache, bump `CACHE_SCHEMA_VERSION` so existing caches re-parse.
- Cube column order is a contract between `token_stats.py` (`cube_columns`, `cube_hourly_columns`) and the `C_*` constants in `web/app.js`. Change both together; append columns rather than reorder.
- Each extractor also returns `timelines` (`{session_id: {parent, calls: [[epoch_ms, model, inp, cr, cw, out, reas, cost, effort], ...]}}`, `effort` = reasoning-effort level or `""` when not logged); `build_session_details` groups them by root session into `web/sessions/<id>.js` and sets each session's `detail` id.
- Cube keys inside extractors use a local `YYYY-MM-DDTHH` bucket (bare date when the hour is unknown); `extract_all` splits them into the daily and hourly cubes.
- The date range can have hour-level edges (`state.hourFrom` / `state.hourTo`). Panels must aggregate `scopeRows().filter(rowMatches)` rather than `rawData.cube` directly, so partial first/last days come from hourly rows.
- Chart colors live in `PALETTES` in `web/common.js` (dark + light), with harness/provider colors mirrored as `--c-*` variables in `styles.css`. Keep them in sync, and check new categorical colors for distinctness (including colour-blind separation) in both themes.

## Running and checking

```bash
python3.14 token_stats.py extract --rebuild   # full re-parse, writes data/ and web/data.js
python3.14 token_stats.py summary             # terminal tables only
python3.14 token_stats.py --open              # serve http://127.0.0.1:8765
node --check web/app.js                       # quick JS syntax check
```

There is no test suite. To verify changes without real logs, point `HOME` at a temp directory containing synthetic logs (e.g. `~/.claude/projects/<slug>/*.jsonl`) and run `extract` from a copy of the repo, then open `web/index.html` and check the browser console for errors.
