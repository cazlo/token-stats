# AGENTS.md

Guidance for AI coding agents working in this repo. See `README.md` for what the tool does.

## Layout

- `token_stats.py` — the whole backend: per-harness extractors (Claude Code, Codex, OpenCode, Antigravity), merge into the `cube` / `cube_hourly` payload, billing ledger, CLI and local HTTP server.
- `web/` — `index.html`, `app.js`, `styles.css`: the dashboard (vanilla JS, inline SVG charts).
- Generated, git-ignored: `data/` (parse cache, `stats.json`, billing overrides) and `web/data.js`. Never commit these — they hold the user's local usage data.

## Hard constraints

- **Zero dependencies.** Python 3.14+ standard library only (it uses `compression.zstd`); no PyPI packages. No npm packages, CDNs or build step in `web/` — the page must work offline and via `file://`.
- **Read-only on sources.** Extractors only read harness logs and databases under `~`; never write to them.
- Keep the server bound to localhost by default.

## Working rules

- If you change what a worker parses or what gets stored in the parse cache, bump `CACHE_SCHEMA_VERSION` so existing caches re-parse.
- Cube column order is a contract between `token_stats.py` (`cube_columns`, `cube_hourly_columns`) and the `C_*` constants in `web/app.js`. Change both together; append columns rather than reorder.
- Cube keys inside extractors use a local `YYYY-MM-DDTHH` bucket (bare date when the hour is unknown); `extract_all` splits them into the daily and hourly cubes.
- Chart colors live in `PALETTES` in `web/app.js` (dark + light), with harness/provider colors mirrored as `--c-*` variables in `styles.css`. Keep them in sync, and check new categorical colors for distinctness (including colour-blind separation) in both themes.

## Running and checking

```bash
python3.14 token_stats.py extract --rebuild   # full re-parse, writes data/ and web/data.js
python3.14 token_stats.py summary             # terminal tables only
python3.14 token_stats.py --open              # serve http://127.0.0.1:8765
node --check web/app.js                       # quick JS syntax check
```

There is no test suite. To verify changes without real logs, point `HOME` at a temp directory containing synthetic logs (e.g. `~/.claude/projects/<slug>/*.jsonl`) and run `extract` from a copy of the repo, then open `web/index.html` and check the browser console for errors.
