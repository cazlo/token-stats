#!/usr/bin/env python3
"""
Local AI Token Spend & Usage Observatory — Extractor, Incremental Cache & Server.

Zero third-party dependencies (100% Python 3.14+ standard library, including
`compression.zstd`, `sqlite3`, `concurrent.futures`, and `http.server`).

Supported local harnesses discovered on this machine:
  1. Claude Code     (~/.claude/projects/**/*.jsonl + pre-cleanup ~/.claude/stats-cache.json)
  2. Codex           (~/.codex/state_5.sqlite + ~/.codex/sessions/**/*.jsonl[.zst] + archived_sessions)
  3. OpenCode        (~/.local/share/opencode/opencode.db: session_v2 + message + session_message)
  4. Antigravity     (~/.gemini/antigravity/conversations/*.db + antigravity-ide + brain/* fallback)
  5. GitHub Copilot  (~/.copilot/state/session-store.db + session-state/*/events.jsonl + VS Code chatSessions)

Usage:
  token-stats [--port 8765] [--open]  # Refresh logs, print summary & serve web UI (default)
  token-stats serve [--port 8765]     # Same as default: extract & serve live web UI
  token-stats summary                 # Extract incrementally & print terminal breakdown tables
  token-stats extract [--rebuild]     # Parse logs -> data/stats.json + web/data.js
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from compression import zstd
from concurrent.futures import ProcessPoolExecutor
import datetime as dt
import errno
import glob
import hashlib
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
import traceback
from typing import Any
from urllib.parse import unquote, urlparse
import urllib.request
import webbrowser

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"
CACHE_DB_PATH = DATA_DIR / "parse_cache.sqlite"
STATS_JSON_PATH = DATA_DIR / "stats.json"
BILLING_OVERRIDES_PATH = DATA_DIR / "billing_overrides.json"
LOCAL_CONFIG_PATH = DATA_DIR / "local_config.json"
SERVER_PID_PATH = DATA_DIR / "server.pid"
WEB_DATA_JS_PATH = WEB_DIR / "data.js"
SESSION_DETAIL_DIR = WEB_DIR / "sessions"
SESSION_DETAIL_MANIFEST_PATH = DATA_DIR / "session_details_manifest.json"

HOME = Path.home()
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo

CACHE_SCHEMA_VERSION = "13"
EXTRACT_LOCK = threading.Lock()
_LOCAL_CFG_CACHE: tuple[int, dict[str, Any]] | None = None


def load_local_config() -> dict[str, Any]:
    """Load optional git-ignored machine/workspace customization from data/local_config.json."""
    global _LOCAL_CFG_CACHE
    try:
        st = LOCAL_CONFIG_PATH.stat()
    except OSError:
        _LOCAL_CFG_CACHE = (0, {})
        return {}
    if _LOCAL_CFG_CACHE is not None and _LOCAL_CFG_CACHE[0] == st.st_mtime_ns:
        return _LOCAL_CFG_CACHE[1]
    try:
        parsed = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = parsed if isinstance(parsed, dict) else {}
    except Exception:
        cfg = {}
    _LOCAL_CFG_CACHE = (st.st_mtime_ns, cfg)
    return cfg


def get_backup_roots() -> list[Path]:
    """
    Return optional backup log root directories (configured via `backup_roots` in
    `data/local_config.json` or `$TOKEN_STATS_BACKUP_DIR`).
    """
    home_str = str(HOME)
    raw_candidates: list[str] = []
    env_b = os.environ.get("TOKEN_STATS_BACKUP_DIR") or ""
    if env_b.strip():
        for part in env_b.split(os.pathsep):
            if part.strip():
                raw_candidates.append(part.strip())
    cfg = load_local_config()
    for item in cfg.get("backup_roots") or []:
        if isinstance(item, str) and item.strip():
            raw_candidates.append(item.strip().replace("{HOME}", home_str))

    out: list[Path] = []
    seen: set[str] = set()
    for rc in raw_candidates:
        p = Path(rc).expanduser()
        if not p.is_dir():
            continue
        key = str(p.resolve())
        if key not in seen and key != str(HOME.resolve()):
            seen.add(key)
            out.append(p)
    return out


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically via same-directory temp file + os.replace to prevent torn reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _file_sig(path: str) -> tuple[int, int] | None:
    """Return (mtime_ns, size) for a file, folding in SQLite -wal state when present."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    mtime_ns = st.st_mtime_ns
    size = st.st_size
    if path.endswith((".db", ".sqlite", ".sqlite3")):
        try:
            wst = os.stat(path + "-wal")
            if wst.st_size > 0:
                mtime_ns = max(mtime_ns, wst.st_mtime_ns)
                size += wst.st_size
        except OSError:
            pass
    return (mtime_ns, size)


# ---------------------------------------------------------------------------
# Normalization helpers (Projects, Models, Providers, Dates)
# ---------------------------------------------------------------------------


def epoch_to_local_date(ts_sec: float | int | None) -> str:
    if not ts_sec or ts_sec <= 0:
        return "unknown"
    if ts_sec > 1e11:
        ts_sec = ts_sec / 1000.0
    try:
        return dt.datetime.fromtimestamp(ts_sec, tz=LOCAL_TZ).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return "unknown"


def epoch_to_local_hour_key(ts_sec: float | int | None) -> str:
    """Local "YYYY-MM-DDTHH" bucket key for an epoch timestamp (seconds or ms), or "unknown"."""
    if not ts_sec or ts_sec <= 0:
        return "unknown"
    if ts_sec > 1e11:
        ts_sec = ts_sec / 1000.0
    try:
        return dt.datetime.fromtimestamp(ts_sec, tz=LOCAL_TZ).strftime("%Y-%m-%dT%H")
    except (OSError, OverflowError, ValueError):
        return "unknown"


def iso_to_local_hour_key(iso_str: str | None) -> str:
    """Local "YYYY-MM-DDTHH" bucket key for an ISO timestamp; falls back to a bare date or "unknown"."""
    if not iso_str:
        return "unknown"
    s = iso_str.strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            parsed = dt.datetime.fromisoformat(s)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(LOCAL_TZ)
            if len(s) <= 10:
                return parsed.strftime("%Y-%m-%d")
            return parsed.strftime("%Y-%m-%dT%H")
        except ValueError:
            return s[:10]
    return "unknown"


def to_epoch_ms(ts: Any) -> int:
    """Epoch milliseconds from an epoch (s or ms) number or an ISO timestamp; 0 when unknown."""
    if ts is None or ts == "":
        return 0
    if isinstance(ts, (int, float)):
        if ts <= 0:
            return 0
        return int(ts if ts > 1e11 else ts * 1000)
    s = str(ts).strip()
    if s.replace(".", "", 1).isdigit():
        return to_epoch_ms(float(s))
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(s)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return int(parsed.timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return 0


def new_timelines() -> dict[str, dict[str, Any]]:
    """Per-session call timelines: {session_id: {"parent": parent_session_id, "calls": [[ms, model, inp, cr, cw, out, reas, cost, effort], ...]}}.

    `effort` is the model's reasoning-effort level for that call ("low", "high", ...) or "" when the harness doesn't log it.
    """
    return defaultdict(lambda: {"parent": "", "calls": []})


def hour_key_date(key: str) -> str:
    """Strip the hour suffix from a "YYYY-MM-DDTHH" (or bare date / "unknown") bucket key."""
    return key[:10] if key and key != "unknown" else "unknown"


def split_hour_key(key: str) -> tuple[str, int]:
    """("YYYY-MM-DD", hour 0..23) from a bucket key; hour is -1 when unknown."""
    if key and len(key) == 13 and key[10] == "T":
        try:
            return key[:10], int(key[11:13])
        except ValueError:
            return key[:10], -1
    return hour_key_date(key), -1


def iso_to_local_date(iso_str: str | None) -> str:
    if not iso_str:
        return "unknown"
    s = iso_str.strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            parsed = dt.datetime.fromisoformat(s)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(LOCAL_TZ)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            return s[:10]
    return "unknown"


def decode_claude_slug_dir(folder_name: str) -> str:
    """Fallback decoder for ~/.claude/projects/<slug> folder names."""
    if "--claude-worktrees-" in folder_name:
        folder_name = folder_name.split("--claude-worktrees-", 1)[0]
    home_str = str(HOME).rstrip("/")
    home_slug = "-" + home_str.lstrip("/").replace("/", "-")
    known_prefixes = [
        (f"{home_slug}-src-", f"{home_str}/src/"),
        (f"{home_slug}-code-", f"{home_str}/code/"),
        (f"{home_slug}-projects-", f"{home_str}/projects/"),
        (f"{home_slug}-", f"{home_str}/"),
    ]
    for pref, repl in known_prefixes:
        if folder_name.startswith(pref):
            return repl + folder_name[len(pref) :]
    return folder_name


def normalize_project(raw_path: str | None) -> tuple[str, str]:
    """
    Map raw working directory / URI to (canonical_project_name, cleaned_raw_path).
    Collapses git worktrees, Claude/OpenCode/Antigravity temp shards, and repo subdirs.
    """
    if not raw_path:
        return ("(unassigned)", "")
    p = raw_path.strip()
    if p.startswith(("file://", "vscode-remote://")):
        p = unquote(urlparse(p).path)
    p = p.rstrip("/")
    ws_stem = ""
    if p.endswith(".code-workspace"):
        ws_stem = os.path.basename(p)[:-15]
        p = os.path.dirname(p).rstrip("/")
    if not p or p == "/":
        if ws_stem:
            alias_map_ws: dict[str, str] = load_local_config().get("project_aliases") or {}
            return (alias_map_ws.get(ws_stem, ws_stem), "/")
        return ("(global / root)", p or "/")
    if "/.config/" in p and (p.endswith("/workspace.json") or p.endswith("/agent-sessions")):
        return ("(unassigned)", "")

    cleaned = p
    for marker in ("/.claude/worktrees/", "/.worktrees/", "/worktrees/"):
        if marker in p and not p.startswith(str(HOME / ".codex/worktrees")) and not p.startswith(
            str(HOME / ".gemini/antigravity/worktrees")
        ):
            p = p.split(marker, 1)[0]

    home_str = str(HOME)
    cfg = load_local_config()
    alias_map: dict[str, str] = cfg.get("project_aliases") or {}

    if ws_stem and p == home_str:
        return (alias_map.get(ws_stem, ws_stem), cleaned)

    for src_dir in ("src", "code", "projects", "repos"):
        src_prefix = f"{home_str}/{src_dir}/"
        if p.startswith(src_prefix):
            rel = p[len(src_prefix) :]
            repo = rel.split("/", 1)[0]
            return (alias_map.get(repo, repo), cleaned)

    low = p.lower()
    for rule in cfg.get("path_rules") or []:
        if not isinstance(rule, dict) or not rule.get("project"):
            continue
        pref = str(rule.get("prefix") or "").replace("{HOME}", home_str)
        cont = str(rule.get("contains") or "").lower()
        if (pref and p.startswith(pref)) or (cont and cont in low):
            return (str(rule["project"]), cleaned)

    if p.startswith(f"{home_str}/Documents/Codex"):
        return ("codex-adhoc", cleaned)
    if p.startswith(f"{home_str}/Documents/Default Project"):
        return ("(default-project)", cleaned)
    if p == home_str:
        return ("(home-dir)", cleaned)
    if p.startswith(f"{home_str}/Downloads"):
        return ("(downloads)", cleaned)

    if p.startswith(home_str + "/"):
        top = p[len(home_str) + 1 :].split("/", 1)[0]
        if not top.startswith("."):
            return (alias_map.get(top, top), cleaned)

    base = os.path.basename(p) or "(other)"
    return (alias_map.get(base, base), cleaned)


EFFORT_SUFFIX_RE = re.compile(
    r"\s*\((?:low|medium|high|xhigh|minimal|thinking|fast|default)\)\s*$",
    re.IGNORECASE,
)
CLAUDE_EFFORT_SLUG_RE = re.compile(
    r"^(claude-(?:opus|sonnet|haiku|fable)-[0-9]+(?:-[0-9]+)*)-(?:low|medium|high|xhigh)$",
    re.IGNORECASE,
)

AG_MODEL_ALIASES: dict[str, str] = {
    "gemini-3.8-flash-tiered": "gemini-3.8-flash",
    "gemini-3p7-flash-exp-c": "gemini-3.7-flash",
    "gemini-4-argon-d": "gemini-4-argon",
    "subagent:pro": "gemini-4-argon",
    "subagent:flash": "gemini-3.8-flash",
    "subagent:flash_lite": "gemini-3.8-flash-lite",
}

AG_ENUM_TO_MODEL: dict[int, str] = {
    1071: "gemini-3.6-flash",
    1298: "gemini-3.7-flash",
    1318: "gemini-3.8-flash",
    1319: "gemini-3.8-flash",
    1322: "gemini-3.8-flash",
    1401: "claude-opus-5-5",
    1402: "claude-opus-5-5",
    1404: "claude-sonnet-5-5",
    1417: "gemini-4-argon",
    1418: "gemini-4-argon",
}


PREVIEW_SUFFIX_RE = re.compile(r"\s*\(preview\)\s*", re.IGNORECASE)
DATE_STAMP_SUFFIX_RE = re.compile(r"-(?:20\d{2}-\d{2}-\d{2}|20\d{6})$")


def normalize_model_and_provider(raw_model: str | None, raw_provider: str | None = None, harness: str = "") -> tuple[str, str]:
    """Return canonical (model_id, provider_label) harmonized across all harnesses."""
    m = (raw_model or "").strip()
    prov = (raw_provider or "").strip().lower()
    is_copilot_source = harness == "GitHub Copilot" or prov in ("github-copilot", "copilot")
    if prov in ("github-copilot", "copilot"):
        prov = ""

    if not m or m == "<synthetic>":
        m = "(unknown)"
    else:
        for v_pref in ("github-copilot/", "copilot/"):
            if m.lower().startswith(v_pref):
                m = m[len(v_pref) :].strip()
                is_copilot_source = True
                break
        ml_check = m.lower()
        if ml_check in AG_MODEL_ALIASES:
            m = AG_MODEL_ALIASES[ml_check]
        elif is_copilot_source:
            stripped = PREVIEW_SUFFIX_RE.sub(" ", EFFORT_SUFFIX_RE.sub("", m)).strip()
            sl = stripped.lower()
            if sl in AG_MODEL_ALIASES:
                m = AG_MODEL_ALIASES[sl]
            elif sl in ("oswe-vscode-prime", "raptor mini", "raptor-mini"):
                m = "raptor-mini"
            elif sl == "claude-(unknown)":
                m = "claude-(unknown)"
            elif sl.startswith("claude ") or sl.startswith("claude-"):
                m = re.sub(r"[\s.]+", "-", sl)
            elif sl.startswith(("gemini ", "gpt ", "gpt-", "grok ")):
                m = re.sub(r"\s+", "-", sl)
            else:
                m = stripped
            m = re.sub(r"-preview$", "", DATE_STAMP_SUFFIX_RE.sub("", m), flags=re.IGNORECASE)
            cm = CLAUDE_EFFORT_SLUG_RE.match(m)
            if cm:
                m = cm.group(1).lower()
        else:
            stripped = EFFORT_SUFFIX_RE.sub("", m).strip()
            sl = stripped.lower()
            if sl in AG_MODEL_ALIASES:
                m = AG_MODEL_ALIASES[sl]
            elif sl.startswith("claude "):
                m = re.sub(r"[\s.]+", "-", sl)
            elif sl.startswith("gemini "):
                m = re.sub(r"\s+", "-", sl)
            else:
                m = stripped
            cm = CLAUDE_EFFORT_SLUG_RE.match(m)
            if cm:
                m = cm.group(1).lower()

    ml = m.lower()
    cfg = load_local_config()
    local_provs = {"local", "ollama", "vllm", "lmstudio", "llamacpp"} | {
        str(x).lower() for x in (cfg.get("local_gpu_providers") or [])
    }
    local_kw = ("qwen", "nemotron", "llama-", "deepseek-", "mistral-") + tuple(
        str(x).lower() for x in (cfg.get("local_gpu_model_keywords") or [])
    )
    if prov in local_provs or any(k in ml for k in local_kw):
        provider = "Local / Self-Hosted GPU"
    elif prov == "anthropic" or "claude" in ml:
        provider = "Anthropic"
    elif prov in ("openai", "custom-gateway") or ml.startswith(("gpt-", "o1", "o3", "o4", "codex-", "raptor-", "oswe-")):
        provider = "OpenAI"
    elif prov in ("google", "gemini") or "gemini" in ml:
        provider = "Google"
    elif prov == "xai" or "grok" in ml:
        provider = "xAI"
    elif prov in ("moonshot", "moonshotai") or "kimi" in ml:
        provider = "Moonshot AI"
    elif prov in ("microsoft", "azure") or ml.startswith("mai-"):
        provider = "Microsoft"
    else:
        if harness == "Claude Code":
            provider = "Anthropic"
        elif harness == "Codex":
            provider = "OpenAI"
        elif harness == "Antigravity":
            provider = "Google"
        else:
            provider = prov.capitalize() if prov else "Other"

    return (m, provider)


# ---------------------------------------------------------------------------
# Incremental File Parse Cache (SQLite)
# ---------------------------------------------------------------------------


def open_cache_db(rebuild: bool = False) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if rebuild and CACHE_DB_PATH.exists():
        CACHE_DB_PATH.unlink()
    con = sqlite3.connect(str(CACHE_DB_PATH), timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    ver = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not ver or ver[0] != CACHE_SCHEMA_VERSION:
        con.executescript(
            """
            DROP TABLE IF EXISTS file_cache;
            DROP TABLE IF EXISTS opencode_msg_cache;
            DROP TABLE IF EXISTS opencode_sess_state;
            DELETE FROM meta;
            """
        )
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (CACHE_SCHEMA_VERSION,),
        )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS file_cache (
            harness TEXT NOT NULL,
            path TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    con.commit()
    return con


def _prune_file_cache(cache_con: sqlite3.Connection, harness: str, active_paths: set[str], cached_paths: set[str]) -> None:
    stale = cached_paths - active_paths
    if stale:
        cache_con.executemany(
            "DELETE FROM file_cache WHERE harness = ? AND path = ?",
            [(harness, p) for p in stale],
        )
        cache_con.commit()


# ---------------------------------------------------------------------------
# 1. Claude Code Parser
# ---------------------------------------------------------------------------


def _claude_rec_rank(rec: list[Any]) -> tuple[int, int, int]:
    """Rank streaming chunks for the same message.id: completed (has_stop=1) + higher output + total wins."""
    has_stop = int(rec[9]) if len(rec) > 9 else 0
    out_and_reas = int(rec[7]) + int(rec[8])
    tot = int(rec[4]) + int(rec[5]) + int(rec[6]) + out_and_reas
    return (has_stop, out_and_reas, tot)


def _parse_claude_file(path: str) -> dict[str, Any]:
    """Worker: scan a single Claude Code .jsonl file and extract deduplicated API calls."""
    is_sub = "/subagents/" in path
    folder_slug = ""
    parts = path.split("/.claude/projects/", 1)
    if len(parts) == 2:
        folder_slug = parts[1].split("/", 1)[0]
    fallback_cwd = decode_claude_slug_dir(folder_slug) if folder_slug else ""

    raw_chunks_by_id: dict[str, tuple[tuple[int, int, int], list[list[Any]]]] = {}
    session_id = ""
    first_prompt = ""
    min_ts = ""
    max_ts = ""

    try:
        with open(path, "rb") as f:
            for raw in f:
                if not first_prompt and b'"user"' in raw and b'"content"' in raw:
                    try:
                        uo = json.loads(raw)
                        if uo.get("type") == "user" and not uo.get("isMeta"):
                            mc = (uo.get("message") or {}).get("content")
                            if isinstance(mc, str) and not mc.startswith("<"):
                                first_prompt = mc.strip().replace("\n", " ")[:120]
                            elif isinstance(mc, list):
                                for blk in mc:
                                    if isinstance(blk, dict) and blk.get("type") == "text":
                                        txt = (blk.get("text") or "").strip()
                                        if txt and not txt.startswith("<"):
                                            first_prompt = txt.replace("\n", " ")[:120]
                                            break
                    except Exception:
                        pass

                if b'"usage"' not in raw:
                    continue
                try:
                    o = json.loads(raw)
                except Exception:
                    continue
                m = o.get("message")
                if not isinstance(m, dict) or "usage" not in m:
                    continue
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue

                inp = int(u.get("input_tokens") or 0)
                cr = int(u.get("cache_read_input_tokens") or 0)
                cw = int(u.get("cache_creation_input_tokens") or 0)
                out_tot = int(u.get("output_tokens") or 0)
                reas = int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
                if inp + cr + cw + out_tot == 0:
                    continue

                mid = str(m.get("id") or o.get("requestId") or o.get("uuid") or f"{path}:{len(raw_chunks_by_id)}")
                ts = o.get("timestamp") or ""
                if ts:
                    if not min_ts or ts < min_ts:
                        min_ts = ts
                    if not max_ts or ts > max_ts:
                        max_ts = ts
                session_id = o.get("sessionId") or session_id
                cwd = o.get("cwd") or fallback_cwd
                actor = "subagent" if (is_sub or o.get("isSidechain")) else "main"
                model = m.get("model") or "unknown"
                has_stop = 1 if m.get("stop_reason") else 0
                # Claude Code logs the session effort setting and the effective per-turn effort
                effort = str(o.get("perTurnEffort") or o.get("effort") or "")

                # Handle Anthropic server-side model fallback (`usage.iterations` with `fallback_message`)
                iters = u.get("iterations")
                sub_recs: list[list[Any]] = []
                if isinstance(iters, list) and len(iters) > 1 and any(
                    isinstance(it, dict) and it.get("type") == "fallback_message" for it in iters
                ):
                    for idx_it, it in enumerate(iters):
                        if not isinstance(it, dict):
                            continue
                        it_in = int(it.get("input_tokens") or 0)
                        it_cr = int(it.get("cache_read_input_tokens") or 0)
                        it_cw = int(it.get("cache_creation_input_tokens") or 0)
                        it_out = int(it.get("output_tokens") or 0)
                        if it_in + it_cr + it_cw + it_out == 0:
                            continue
                        it_model = str(it.get("model") or model)
                        it_reas = reas if idx_it == len(iters) - 1 and reas <= it_out else 0
                        sub_recs.append(
                            [f"{mid}#iter{idx_it}", it_model, ts, cwd, actor, it_in, it_cr, it_cw, max(0, it_out - it_reas), it_reas, has_stop, effort]
                        )
                if not sub_recs:
                    sub_recs = [[mid, model, ts, cwd, actor, inp, cr, cw, max(0, out_tot - reas), reas, has_stop, effort]]

                tot_out = sum(r[8] + r[9] for r in sub_recs)
                tot_all = sum(r[5] + r[6] + r[7] + r[8] + r[9] for r in sub_recs)
                rank = (has_stop, tot_out, tot_all)
                prev = raw_chunks_by_id.get(mid)
                if prev is None or rank >= prev[0]:
                    raw_chunks_by_id[mid] = (rank, sub_recs)
    except OSError:
        pass

    calls_by_id: dict[str, list[Any]] = {}
    for _rank, sub_recs in raw_chunks_by_id.values():
        for sr in sub_recs:
            calls_by_id[sr[0]] = sr[1:]

    if not session_id:
        session_id = Path(path).stem
    run_id = f"{session_id}::{Path(path).stem}" if is_sub else session_id

    return {
        "session_id": run_id,
        "root_session_id": session_id,
        "is_sub": is_sub,
        "cwd": fallback_cwd,
        "title": first_prompt,
        "min_ts": min_ts,
        "max_ts": max_ts,
        "calls": calls_by_id,
    }


def _iso_to_utc_date(iso_str: str | None) -> str:
    if not iso_str:
        return "unknown"
    s = iso_str.strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            parsed = dt.datetime.fromisoformat(s)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(dt.timezone.utc)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            return s[:10]
    return "unknown"


def extract_claude(cache_con: sqlite3.Connection, pool: ProcessPoolExecutor | None) -> dict[str, Any]:
    t0 = time.time()
    claude_dir = HOME / ".claude"
    primary_prefix = str(claude_dir.resolve()) + os.sep
    claude_dirs: list[Path] = []
    seen_cdirs: set[str] = set()
    for cd in [claude_dir, *[b / ".claude" for b in get_backup_roots()]]:
        if cd.is_dir():
            rk = str(cd.resolve())
            if rk not in seen_cdirs:
                seen_cdirs.add(rk)
                claude_dirs.append(cd)

    # Deterministic order: main session files before /subagents/, live ~/.claude before backups on ties
    resolved_by_path: dict[str, str] = {}
    raw_jfiles: list[str] = []
    seen_jpaths: set[str] = set()
    for cd in claude_dirs:
        pdir = cd / "projects"
        if pdir.is_dir():
            for jf in glob.glob(str(pdir / "**" / "*.jsonl"), recursive=True):
                rj = str(Path(jf).resolve())
                if rj not in seen_jpaths:
                    seen_jpaths.add(rj)
                    resolved_by_path[jf] = rj
                    raw_jfiles.append(jf)
    jfiles = sorted(
        raw_jfiles,
        key=lambda p: (
            1 if "/subagents/" in p else 0,
            0 if resolved_by_path[p].startswith(primary_prefix) else 1,
            p,
        ),
    )

    cached_rows = {
        path: (mtime_ns, size, payload)
        for path, mtime_ns, size, payload in cache_con.execute(
            "SELECT path, mtime_ns, size, payload_json FROM file_cache WHERE harness='claude'"
        )
    }

    to_parse: list[tuple[str, int, int]] = []
    parsed_payloads: list[tuple[str, dict[str, Any]]] = []
    active_paths: set[str] = set()
    cache_hits = 0

    for p in jfiles:
        sig = _file_sig(p)
        if sig is None:
            continue
        active_paths.add(p)
        mtime_ns, size = sig
        c = cached_rows.get(p)
        if c and c[0] == mtime_ns and c[1] == size:
            parsed_payloads.append((p, json.loads(c[2])))
            cache_hits += 1
        else:
            to_parse.append((p, mtime_ns, size))

    if to_parse:
        paths_only = [x[0] for x in to_parse]
        fresh = list(pool.map(_parse_claude_file, paths_only, chunksize=20)) if pool else [_parse_claude_file(p) for p in paths_only]
        inserts = []
        for (p, mtime_ns, size), payload in zip(to_parse, fresh):
            parsed_payloads.append((p, payload))
            inserts.append(("claude", p, mtime_ns, size, json.dumps(payload, separators=(",", ":"))))
        cache_con.executemany(
            "INSERT OR REPLACE INTO file_cache(harness, path, mtime_ns, size, payload_json) VALUES (?, ?, ?, ?, ?)",
            inserts,
        )
        cache_con.commit()

    _prune_file_cache(cache_con, "claude", active_paths, set(cached_rows.keys()))
    parsed_payloads.sort(
        key=lambda item: (
            1 if "/subagents/" in item[0] else 0,
            0 if resolved_by_path.get(item[0], "").startswith(primary_prefix) else 1,
            item[0],
        )
    )

    # Global deduplication across all Claude JSONL files by message.id
    global_calls: dict[str, tuple[str, list[Any], bool]] = {}
    session_meta: dict[str, dict[str, Any]] = {}

    for path, payload in parsed_payloads:
        is_primary = resolved_by_path.get(path, "").startswith(primary_prefix)
        sid = payload["session_id"]
        if sid not in session_meta:
            session_meta[sid] = {
                "title": payload.get("title") or "",
                "cwd": payload.get("cwd") or "",
                "is_sub": payload.get("is_sub", False),
                "max_ts": payload.get("max_ts") or "",
                "root": payload.get("root_session_id") or sid,
            }
        else:
            if not session_meta[sid]["title"] and payload.get("title"):
                session_meta[sid]["title"] = payload["title"]
            if (payload.get("max_ts") or "") > session_meta[sid]["max_ts"]:
                session_meta[sid]["max_ts"] = payload["max_ts"]

        for mid, rec in payload["calls"].items():
            prev = global_calls.get(mid)
            # Strict `>` on cross-file ties keeps deterministic first-seen main session owner
            if prev is None or _claude_rec_rank(rec) > _claude_rec_rank(prev[1]):
                global_calls[mid] = (sid, rec, is_primary)

    cube_acc: dict[tuple[str, str, str, str, str, str, int], list[float]] = defaultdict(
        lambda: [0, 0, 0, 0, 0, 0, 0.0]
    )
    sess_acc: dict[str, dict[str, Any]] = {}
    min_live_date = "9999-99-99"
    max_live_date = "0000-00-00"
    min_primary_live_date = "9999-99-99"
    covered_dates: set[str] = set()
    project_raw_paths: dict[str, set[str]] = defaultdict(set)

    timelines = new_timelines()
    for sid, rec, is_primary in global_calls.values():
        model_raw, ts, cwd, actor, inp, cr, cw, out_txt, reas = rec[:9]
        hour_key = iso_to_local_hour_key(ts)
        date_str = hour_key_date(hour_key)
        utc_d = _iso_to_utc_date(ts)
        if utc_d != "unknown":
            covered_dates.add(utc_d)
        if date_str != "unknown":
            covered_dates.add(date_str)
            if date_str < min_live_date:
                min_live_date = date_str
            if date_str > max_live_date:
                max_live_date = date_str
            if is_primary and date_str < min_primary_live_date:
                min_primary_live_date = date_str
        proj, clean_path = normalize_project(cwd)
        if clean_path:
            project_raw_paths[proj].add(clean_path)
        model, provider = normalize_model_and_provider(model_raw, "anthropic", "Claude Code")

        key = (hour_key, "Claude Code", provider, model, proj, actor, 0)
        b = cube_acc[key]
        b[0] += 1
        b[1] += inp
        b[2] += cr
        b[3] += cw
        b[4] += out_txt
        b[5] += reas
        call_effort = str(rec[10]) if len(rec) > 10 else ""
        timelines[sid]["calls"].append([to_epoch_ms(ts), model, inp, cr, cw, out_txt, reas, 0.0, call_effort])

        s = sess_acc.get(sid)
        if s is None:
            meta = session_meta.get(sid, {})
            s = {
                "id": sid,
                "harness": "Claude Code",
                "title": meta.get("title") or f"Claude session {sid[:8]}",
                "project": proj,
                "model": model,
                "provider": provider,
                "actor": actor,
                "date": date_str,
                "calls": 0,
                "input_uncached": 0,
                "cache_read": 0,
                "cache_write": 0,
                "output_text": 0,
                "reasoning": 0,
                "cost_usd": 0.0,
                "_mp_toks": defaultdict(int),
                "_mp_max_date": {},
            }
            sess_acc[sid] = s
        call_toks = inp + cr + cw + out_txt + reas
        mp = (model, provider, proj, actor)
        s["_mp_toks"][mp] += call_toks
        prev_d = s["_mp_max_date"].get(mp)
        if prev_d is None or (date_str != "unknown" and (prev_d == "unknown" or date_str > prev_d)):
            s["_mp_max_date"][mp] = date_str
        s["calls"] += 1
        s["input_uncached"] += inp
        s["cache_read"] += cr
        s["cache_write"] += cw
        s["output_text"] += out_txt
        s["reasoning"] += reas

    for tl_sid, tl in timelines.items():
        root = (session_meta.get(tl_sid) or {}).get("root") or tl_sid
        tl["parent"] = root if root != tl_sid else ""

    for s in sess_acc.values():
        dom_mp = max(s["_mp_toks"].items(), key=lambda kv: kv[1])[0]
        s["model"], s["provider"], s["project"], s["actor"] = dom_mp
        s["date"] = s["_mp_max_date"].get(dom_mp, s["date"])
        s.pop("_mp_toks", None)
        s.pop("_mp_max_date", None)

    # Pre-cleanup ~/.claude/stats-cache.json ONLY for gap UTC dates not already covered by .jsonl files
    archived_days = 0
    seen_sc_dates: set[str] = set()
    for cd in claude_dirs:
        sc_path = cd / "stats-cache.json"
        if not sc_path.exists():
            continue
        try:
            sc = json.loads(sc_path.read_text(encoding="utf-8"))
            model_usage = sc.get("modelUsage") or {}
            ratios: dict[str, tuple[float, float, float, float]] = {}
            for m_name, mu in model_usage.items():
                i_t = float(mu.get("inputTokens") or 0)
                cr_t = float(mu.get("cacheReadInputTokens") or 0)
                cw_t = float(mu.get("cacheCreationInputTokens") or 0)
                o_t = float(mu.get("outputTokens") or 0)
                tot_m = i_t + cr_t + cw_t + o_t
                if tot_m > 0:
                    ratios[m_name] = (i_t / tot_m, cr_t / tot_m, cw_t / tot_m, o_t / tot_m)

            daily_act = {d["date"]: d for d in (sc.get("dailyActivity") or []) if "date" in d}
            for entry in sc.get("dailyModelTokens") or []:
                d_str = entry.get("date") or ""
                if (
                    not d_str
                    or d_str in covered_dates
                    or d_str in seen_sc_dates
                    or d_str >= min_primary_live_date
                ):
                    continue
                seen_sc_dates.add(d_str)
                archived_days += 1
                tbm = entry.get("tokensByModel") or {}
                day_msgs = int((daily_act.get(d_str) or {}).get("messageCount") or len(tbm))
                tot_day_tok = sum(tbm.values()) or 1
                for m_raw, tok_total in tbm.items():
                    if tok_total <= 0:
                        continue
                    r_in, r_cr, r_cw, r_out = ratios.get(m_raw, (0.01, 0.96, 0.02, 0.01))
                    inp = round(tok_total * r_in)
                    cr = round(tok_total * r_cr)
                    cw = round(tok_total * r_cw)
                    out_txt = max(0, int(tok_total) - inp - cr - cw)
                    est_calls = max(1, round(day_msgs * (tok_total / tot_day_tok)))
                    model, provider = normalize_model_and_provider(m_raw, "anthropic", "Claude Code")
                    key = (d_str, "Claude Code", provider, model, "(pre-cleanup claude history)", "main", 1)
                    b = cube_acc[key]
                    b[0] += est_calls
                    b[1] += inp
                    b[2] += cr
                    b[3] += cw
                    b[4] += out_txt
        except Exception:
            pass

    # Extract strictly non-sensitive subscription & Extra Usage metadata from ~/.claude.json (+ backups)
    claude_billing_meta: dict[str, Any] = {
        "billing_type": "",
        "subscription_created_date": "",
        "extra_usage_enabled": False,
        "fable_overage_consent": False,
        "fable_overage_models": [],
        "fable_migration_date": "",
        "aug_snapshot": None,
        "snapshots": [],
    }
    cj_candidates: list[Path] = []
    if (HOME / ".claude.json").is_file():
        cj_candidates.append(HOME / ".claude.json")
    for cd in claude_dirs:
        if (cd.parent / ".claude.json").is_file() and (cd.parent / ".claude.json") not in cj_candidates:
            cj_candidates.append(cd.parent / ".claude.json")
        bdir = cd / "backups"
        if bdir.is_dir():
            cj_candidates.extend(sorted(bdir.glob(".claude.json.backup*"), reverse=True))

    snap_by_date: dict[str, dict[str, Any]] = {}
    for cj_path in cj_candidates:
        try:
            cj = json.loads(cj_path.read_text(encoding="utf-8", errors="replace"))
            if not isinstance(cj, dict):
                continue
            oauth = cj.get("oauthAccount") or {}
            if isinstance(oauth, dict):
                if not claude_billing_meta["billing_type"] and oauth.get("billingType"):
                    claude_billing_meta["billing_type"] = str(oauth.get("billingType") or "")
                if not claude_billing_meta["subscription_created_date"] and oauth.get("subscriptionCreatedAt"):
                    claude_billing_meta["subscription_created_date"] = iso_to_local_date(
                        oauth.get("subscriptionCreatedAt")
                    )
                if oauth.get("hasExtraUsageEnabled"):
                    claude_billing_meta["extra_usage_enabled"] = True
            fov = cj.get("fableOverageConsentV2")
            if isinstance(fov, dict) and any(bool(v) for v in fov.values()):
                claude_billing_meta["fable_overage_consent"] = True
            gb = cj.get("cachedGrowthBookFeatures") or {}
            if isinstance(gb, dict) and not claude_billing_meta["fable_overage_models"]:
                inc = gb.get("tengu_usage_overage_included_models")
                if isinstance(inc, list):
                    claude_billing_meta["fable_overage_models"] = [str(x) for x in inc]
            fmig = cj.get("fable5ToFableAliasMigrationTimestamp")
            if isinstance(fmig, (int, float)) and fmig > 0 and not claude_billing_meta["fable_migration_date"]:
                claude_billing_meta["fable_migration_date"] = epoch_to_local_date(fmig)
            cu = cj.get("cachedUsageUtilization")
            if isinstance(cu, dict) and cu.get("fetchedAtMs"):
                ut = cu.get("utilization") or {}
                eu = cu.get("extra_usage") or ut.get("extra_usage") or {}
                fable_scoped_pct = None
                for lim in cu.get("limits") or []:
                    if isinstance(lim, dict):
                        mname = (((lim.get("scope") or {}).get("model") or {}).get("display_name") or "")
                        if "fable" in str(mname).lower():
                            fable_scoped_pct = lim.get("percent")
                s_date = epoch_to_local_date(cu.get("fetchedAtMs"))
                snap_obj = {
                    "date": s_date,
                    "extra_usage_enabled": bool(eu.get("is_enabled")),
                    "monthly_limit_usd": round(float(eu.get("monthly_limit") or 0) / 100.0, 2),
                    "used_credits_usd": round(float(eu.get("used_credits") or 0) / 100.0, 2),
                    "five_hour_pct": (ut.get("five_hour") or {}).get("utilization"),
                    "seven_day_pct": (ut.get("seven_day") or {}).get("utilization"),
                    "fable_weekly_pct": fable_scoped_pct,
                }
                prev_s = snap_by_date.get(s_date)
                if prev_s is None or snap_obj["used_credits_usd"] > prev_s["used_credits_usd"]:
                    snap_by_date[s_date] = snap_obj
                if claude_billing_meta["aug_snapshot"] is None:
                    claude_billing_meta["aug_snapshot"] = snap_obj
        except Exception:
            pass
    claude_billing_meta["snapshots"] = [snap_by_date[k] for k in sorted(snap_by_date.keys())]

    elapsed_ms = round((time.time() - t0) * 1000, 1)
    return {
        "cube": cube_acc,
        "sessions": list(sess_acc.values()),
        "timelines": timelines,
        "project_paths": project_raw_paths,
        "billing_meta": claude_billing_meta,
        "health": {
            "harness": "Claude Code",
            "source_path": "~/.claude/projects + ~/.claude/stats-cache.json",
            "files_total": len(jfiles),
            "cache_hits": cache_hits,
            "files_parsed": len(to_parse),
            "sessions_count": len(sess_acc),
            "api_calls": len(global_calls),
            "archived_days_recovered": archived_days,
            "live_date_range": f"{min_live_date} .. {max_live_date}" if global_calls else "n/a",
            "elapsed_ms": elapsed_ms,
        },
    }


# ---------------------------------------------------------------------------
# 2. Codex Parser (with Forked Subagent & Guardian Parent-Prefix Deduplication)
# ---------------------------------------------------------------------------


def _parse_codex_rollout(path: str) -> dict[str, Any]:
    """
    Worker: extract session_meta, compaction baseline records, and raw token_count
    cumulative tuples so extract_codex can strip inherited parent fork prefixes.
    """
    opener = zstd.open if path.endswith(".zst") else open
    cur_model = ""
    cur_effort = ""
    cur_cwd = ""
    meta_info: dict[str, Any] = {}
    compacted_totals: set[int] = set()
    events: list[list[Any]] = []
    rl_samples: list[list[Any]] = []
    last_rl_state: tuple[float | None, str] | None = None
    last_rl_row: list[Any] | None = None
    last_tot_in_file = -1
    first_prompt = ""

    try:
        with opener(path, "rb") as f:
            for raw in f:
                if b'"session_meta"' in raw and not meta_info:
                    try:
                        ev = json.loads(raw)
                        if ev.get("type") == "session_meta":
                            pl = ev.get("payload") or {}
                            src = pl.get("source")
                            meta_info = {
                                "id": pl.get("id") or pl.get("session_id") or "",
                                "forked_from_id": pl.get("forked_from_id") or "",
                                "parent_thread_id": pl.get("parent_thread_id") or "",
                                "timestamp": pl.get("timestamp") or ev.get("timestamp") or "",
                                "cwd": pl.get("cwd") or "",
                                "source": json.dumps(src) if isinstance(src, dict) else str(src or ""),
                                "thread_source": str(pl.get("thread_source") or ""),
                                "agent_role": str(pl.get("agent_role") or pl.get("agent_nickname") or ""),
                                "model_provider": str(pl.get("model_provider") or ""),
                            }
                            if meta_info["cwd"]:
                                cur_cwd = meta_info["cwd"]
                    except Exception:
                        pass
                elif b'"compacted"' in raw:
                    try:
                        ev = json.loads(raw)
                        if ev.get("type") == "compacted":
                            pl = ev.get("payload") or {}
                            ltr = pl.get("latest_token_usage_record")
                            if isinstance(ltr, dict):
                                tu = ltr.get("total_token_usage") or ltr
                                ct = int(tu.get("total_tokens") or 0)
                                if ct > 0:
                                    compacted_totals.add(ct)
                    except Exception:
                        pass
                elif b'"turn_context"' in raw:
                    try:
                        ev = json.loads(raw)
                        if ev.get("type") == "turn_context":
                            pl = ev.get("payload") or {}
                            if pl.get("model"):
                                cur_model = pl["model"]
                            eff = pl.get("effort") or pl.get("reasoning_effort")
                            if eff:
                                cur_effort = str(eff)
                            if pl.get("cwd") and not cur_cwd:
                                cur_cwd = pl["cwd"]
                    except Exception:
                        pass
                elif not first_prompt and b'"user_message"' in raw:
                    try:
                        ev = json.loads(raw)
                        pl = ev.get("payload") or {}
                        if pl.get("type") == "user_message" and pl.get("message"):
                            first_prompt = str(pl["message"]).strip().replace("\n", " ")[:120]
                    except Exception:
                        pass
                elif b'"token_count"' in raw:
                    try:
                        ev = json.loads(raw)
                        if ev.get("type") != "event_msg":
                            continue
                        pl = ev.get("payload") or {}
                        if pl.get("type") != "token_count":
                            continue
                        rl = pl.get("rate_limits")
                        if isinstance(rl, dict) and rl.get("limit_id") == "codex":
                            pt = str(rl.get("plan_type") or "")
                            cred = rl.get("credits")
                            bal = (
                                float(cred["balance"])
                                if isinstance(cred, dict) and cred.get("balance") is not None
                                else None
                            )
                            p_pct = float((rl.get("primary") or {}).get("used_percent") or 0.0)
                            st_rl = (bal, pt)
                            rl_row = [str(ev.get("timestamp") or ""), bal, pt, p_pct, cur_model, cur_cwd]
                            if st_rl != last_rl_state:
                                rl_samples.append(rl_row)
                                last_rl_state = st_rl
                            last_rl_row = rl_row
                        info = pl.get("info")
                        if not isinstance(info, dict):
                            continue
                        tot = info.get("total_token_usage")
                        if not isinstance(tot, dict):
                            continue
                        c_in = int(tot.get("input_tokens") or 0)
                        c_cr = int(tot.get("cached_input_tokens") or 0)
                        c_cw = int(tot.get("cache_write_input_tokens") or 0)
                        c_out = int(tot.get("output_tokens") or 0)
                        c_reas = int(tot.get("reasoning_output_tokens") or 0)
                        c_tot = int(tot.get("total_tokens") or (c_in + c_out))

                        if c_tot == last_tot_in_file or c_tot <= 0:
                            continue
                        last_tot_in_file = c_tot

                        ltu = info.get("last_token_usage") or {}
                        l_in = int(ltu.get("input_tokens") or 0)
                        l_cr = int(ltu.get("cached_input_tokens") or 0)
                        l_cw = int(ltu.get("cache_write_input_tokens") or 0)
                        l_out = int(ltu.get("output_tokens") or 0)
                        l_reas = int(ltu.get("reasoning_output_tokens") or 0)
                        l_tot = int(ltu.get("total_tokens") or (l_in + l_out))

                        date_str = iso_to_local_hour_key(ev.get("timestamp"))
                        is_comp_base = 1 if c_tot in compacted_totals else 0
                        events.append(
                            [
                                date_str,
                                cur_model,
                                c_in,
                                c_cr,
                                c_cw,
                                c_out,
                                c_reas,
                                c_tot,
                                l_in,
                                l_cr,
                                l_cw,
                                l_out,
                                l_reas,
                                l_tot,
                                is_comp_base,
                                str(ev.get("timestamp") or ""),
                                cur_effort,
                            ]
                        )
                    except Exception:
                        pass
    except Exception:
        pass

    if last_rl_row and (not rl_samples or rl_samples[-1][0] != last_rl_row[0]):
        rl_samples.append(last_rl_row)

    return {
        "cwd": cur_cwd,
        "meta": meta_info,
        "prompt": first_prompt,
        "events": events,
        "rl_samples": rl_samples,
    }


def _resolve_codex_rollout_file(
    rollout_path: str, codex_dir: Path, archived_by_name: dict[str, str]
) -> str | None:
    if not rollout_path:
        return None
    # Prefer resolving relative to `codex_dir` first so relocated backup roots resolve to their own files
    for marker in ("/sessions/", "/archived_sessions/"):
        if marker in rollout_path:
            rel = rollout_path.split(marker, 1)[1]
            sub = marker.strip("/")
            cand = codex_dir / sub / rel
            if cand.exists():
                return str(cand)
            cand_z = Path(str(cand) + ".zst")
            if cand_z.exists():
                return str(cand_z)
    base = os.path.basename(rollout_path)
    if base in archived_by_name:
        return archived_by_name[base]
    if (base + ".zst") in archived_by_name:
        return archived_by_name[base + ".zst"]
    if os.path.exists(rollout_path):
        return rollout_path
    zpath = rollout_path + ".zst"
    if os.path.exists(zpath):
        return zpath
    return None


def _iso_to_epoch_ms(iso_str: str | None) -> int:
    if not iso_str:
        return 0
    try:
        s = iso_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0


def extract_codex(cache_con: sqlite3.Connection, pool: ProcessPoolExecutor | None) -> dict[str, Any]:
    t0 = time.time()
    codex_dirs: list[Path] = []
    seen_codex_dirs: set[Path] = set()
    for cand in [
        HOME / ".codex",
        HOME / ".codex-bak",
        *[b / sub for b in get_backup_roots() for sub in (".codex", ".codex-bak")],
    ]:
        if not cand.is_dir():
            continue
        try:
            rp = cand.resolve()
        except OSError:
            rp = cand
        if rp not in seen_codex_dirs:
            seen_codex_dirs.add(rp)
            codex_dirs.append(cand)

    has_any_source = any(
        (d / "state_5.sqlite").exists() or (d / "sessions").exists() or (d / "archived_sessions").exists()
        for d in codex_dirs
    )
    if not has_any_source:
        return {"cube": {}, "sessions": [], "project_paths": {}, "health": {"harness": "Codex", "files_total": 0}}

    threads_by_id: dict[str, tuple[Any, ...]] = {}
    candidates_by_tid: dict[str, list[str]] = defaultdict(list)
    referenced_paths: set[str] = set()
    disk_rollouts: list[str] = []

    wanted_cols = (
        "id",
        "rollout_path",
        "created_at",
        "updated_at",
        "source",
        "model_provider",
        "cwd",
        "title",
        "tokens_used",
        "model",
        "reasoning_effort",
        "agent_role",
        "thread_source",
        "attached_thread_parent_id",
    )

    for cdir in codex_dirs:
        arch_dir = cdir / "archived_sessions"
        archived_by_name: dict[str, str] = {}
        if arch_dir.exists():
            for entry in sorted(os.listdir(arch_dir)):
                archived_by_name[entry] = str(arch_dir / entry)

        db_path = cdir / "state_5.sqlite"
        if db_path.exists():
            try:
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                tables = {
                    r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                spawn_parents: dict[str, str] = {}
                if "thread_spawn_edges" in tables:
                    e_cols = {
                        r[1] for r in con.execute("PRAGMA table_info(thread_spawn_edges)").fetchall()
                    }
                    if {"parent_thread_id", "child_thread_id"}.issubset(e_cols):
                        for p_id, ch_id in con.execute(
                            "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
                        ):
                            if ch_id and p_id:
                                spawn_parents[str(ch_id)] = str(p_id)

                if "threads" in tables:
                    t_cols = {r[1] for r in con.execute("PRAGMA table_info(threads)").fetchall()}
                    sel_exprs = [c if c in t_cols else f"NULL AS {c}" for c in wanted_cols]
                    for raw_row in con.execute(f"SELECT {', '.join(sel_exprs)} FROM threads"):
                        tid = str(raw_row[0] or "")
                        if not tid:
                            continue
                        parent_val = str(raw_row[13] or spawn_parents.get(tid) or "")
                        norm_row = (*raw_row[:13], parent_val)
                        if tid not in threads_by_id:
                            threads_by_id[tid] = norm_row
                        elif not threads_by_id[tid][13] and parent_val:
                            prev_r = threads_by_id[tid]
                            threads_by_id[tid] = (*prev_r[:13], parent_val)

                        actual = _resolve_codex_rollout_file(str(raw_row[1] or ""), cdir, archived_by_name)
                        if actual:
                            referenced_paths.add(actual)
                            if actual not in candidates_by_tid[tid]:
                                candidates_by_tid[tid].append(actual)
                con.close()
            except sqlite3.Error:
                pass

        sess_dir = cdir / "sessions"
        if sess_dir.exists():
            disk_rollouts.extend(sorted(glob.glob(str(sess_dir / "**" / "*.jsonl*"), recursive=True)))
        if arch_dir.exists():
            disk_rollouts.extend(sorted(glob.glob(str(arch_dir / "*.jsonl*"))))

    # Also discover orphan rollout files on disk (or unreferenced candidate rollouts for known threads)
    orphan_files: list[str] = []
    for p in disk_rollouts:
        if p in referenced_paths:
            continue
        name = os.path.basename(p)
        if name.endswith(".jsonl.zst"):
            stem = name[:-10]
        elif name.endswith(".jsonl"):
            stem = name[:-6]
        else:
            continue
        maybe_tid = stem[-36:] if len(stem) >= 36 else stem
        if maybe_tid in threads_by_id:
            if p not in candidates_by_tid[maybe_tid]:
                candidates_by_tid[maybe_tid].append(p)
            continue
        orphan_files.append(p)

    cached_rows = {
        path: (mtime_ns, size, payload)
        for path, mtime_ns, size, payload in cache_con.execute(
            "SELECT path, mtime_ns, size, payload_json FROM file_cache WHERE harness='codex'"
        )
    }

    all_rollout_paths = sorted(
        {p for cands in candidates_by_tid.values() for p in cands} | set(orphan_files)
    )
    to_parse_map: dict[str, tuple[int, int]] = {}
    file_payloads: dict[str, dict[str, Any]] = {}
    active_paths: set[str] = set()
    cache_hits = 0

    for actual in all_rollout_paths:
        sig = _file_sig(actual)
        if sig is None:
            continue
        active_paths.add(actual)
        mtime_ns, size = sig
        c = cached_rows.get(actual)
        if c and c[0] == mtime_ns and c[1] == size:
            file_payloads[actual] = json.loads(c[2])
            cache_hits += 1
        else:
            to_parse_map[actual] = (mtime_ns, size)

    if to_parse_map:
        paths_only = list(to_parse_map.keys())
        fresh = list(pool.map(_parse_codex_rollout, paths_only, chunksize=20)) if pool else [_parse_codex_rollout(p) for p in paths_only]
        inserts = []
        for p, payload in zip(paths_only, fresh):
            mtime_ns, size = to_parse_map[p]
            file_payloads[p] = payload
            inserts.append(("codex", p, mtime_ns, size, json.dumps(payload, separators=(",", ":"))))
        cache_con.executemany(
            "INSERT OR REPLACE INTO file_cache(harness, path, mtime_ns, size, payload_json) VALUES (?, ?, ?, ?, ?)",
            inserts,
        )
        cache_con.commit()

    _prune_file_cache(cache_con, "codex", active_paths, set(cached_rows.keys()))

    # Synthesize thread entries for orphan rollout files discovered on disk
    for opath in orphan_files:
        pl = file_payloads.get(opath)
        if not pl:
            continue
        m_info = pl.get("meta") or {}
        name = os.path.basename(opath)
        stem = name[:-10] if name.endswith(".jsonl.zst") else name[:-6]
        tid = m_info.get("id") or (stem[-36:] if len(stem) >= 36 else stem)
        if opath not in candidates_by_tid[tid]:
            candidates_by_tid[tid].append(opath)
        if tid in threads_by_id:
            continue
        created_ms = _iso_to_epoch_ms(m_info.get("timestamp"))
        threads_by_id[tid] = (
            tid,
            opath,
            created_ms,
            created_ms,
            m_info.get("source") or "",
            m_info.get("model_provider") or "openai",
            m_info.get("cwd") or pl.get("cwd") or "",
            pl.get("prompt") or "",
            0,
            "",
            "",
            m_info.get("agent_role") or "",
            m_info.get("thread_source") or "",
            m_info.get("parent_thread_id") or m_info.get("forked_from_id") or "",
        )

    # Pick the richest rollout file per thread (recovering pre-compaction turns from backups while
    # appending any strictly newer turns recorded in sibling/live rollouts after the backup snapshot)
    thread_files: dict[str, str] = {}
    for tid, cands in candidates_by_tid.items():
        valid_cands = [p for p in cands if p in file_payloads]
        if valid_cands:
            best_p = max(
                valid_cands,
                key=lambda p: len((file_payloads[p].get("events")) or []),
            )
            thread_files[tid] = best_p
            if len(valid_cands) > 1:
                base_pl = file_payloads[best_p]
                base_evs = list(base_pl.get("events") or [])
                base_max_ts = max((str(e[15]) for e in base_evs if len(e) > 15 and e[15]), default="")
                if base_max_ts:
                    extra_evs: list[list[Any]] = []
                    seen_ev_keys = {(str(e[15]), int(e[7])) for e in base_evs if len(e) > 15}
                    for sib_p in valid_cands:
                        if sib_p == best_p:
                            continue
                        for e in file_payloads[sib_p].get("events") or []:
                            e_ts = str(e[15]) if len(e) > 15 else ""
                            ek = (e_ts, int(e[7]))
                            if e_ts > base_max_ts and ek not in seen_ev_keys:
                                seen_ev_keys.add(ek)
                                extra_evs.append(e)
                    if extra_evs:
                        extra_evs.sort(key=lambda e: str(e[15]))
                        file_payloads[best_p] = {**base_pl, "events": base_evs + extra_evs}

    def _classify_codex_actor(row: tuple[Any, ...], payload: dict[str, Any] | None) -> str:
        src_str = str(row[4] or "")
        db_model = str(row[9] or "")
        thread_source = str(row[12] or "")
        parent_id = str(row[13] or "")
        m_info = (payload.get("meta") if payload else None) or {}
        fork_id = m_info.get("forked_from_id") or m_info.get("parent_thread_id") or ""
        if thread_source == "guardian_review" or "guardian" in src_str or db_model == "codex-auto-review":
            return "auto_review"
        if thread_source == "subagent" or "subagent" in src_str or parent_id or fork_id:
            return "subagent"
        return "main"

    def _created_ms(row: tuple[Any, ...], payload: dict[str, Any] | None) -> int:
        c = int(row[2] or 0)
        if c and c < 10_000_000_000:
            c *= 1000
        if not c and payload:
            c = _iso_to_epoch_ms((payload.get("meta") or {}).get("timestamp"))
        return c

    # Process main/earlier parent threads strictly before forked subagent & guardian children
    ordered_threads = sorted(
        threads_by_id.values(),
        key=lambda r: (
            _created_ms(r, file_payloads.get(thread_files.get(r[0], ""))),
            0 if _classify_codex_actor(r, file_payloads.get(thread_files.get(r[0], ""))) == "main" else 1,
            r[0],
        ),
    )

    seen_cum: set[tuple[int, int, int, int, int, int]] = set()
    timelines = new_timelines()
    cube_acc: dict[tuple[str, str, str, str, str, str, int], list[float]] = defaultdict(
        lambda: [0, 0, 0, 0, 0, 0, 0.0]
    )
    sessions_out: list[dict[str, Any]] = []
    project_raw_paths: dict[str, set[str]] = defaultdict(set)
    total_turns = 0
    min_date, max_date = "9999-99-99", "0000-00-00"

    for row in ordered_threads:
        (
            tid,
            _rpath,
            created_at,
            updated_at,
            _source,
            model_provider,
            cwd,
            title,
            tokens_used,
            db_model,
            _effort,
            agent_role,
            _thread_source,
            _parent_id,
        ) = row
        tl_meta = ((file_payloads.get(thread_files.get(tid, "")) or {}).get("meta")) or {}
        tl_parent = str(_parent_id or tl_meta.get("parent_thread_id") or tl_meta.get("forked_from_id") or "")

        actual = thread_files.get(tid)
        payload = file_payloads.get(actual) if actual else None
        actor = _classify_codex_actor(row, payload)

        eff_cwd = cwd or (payload.get("cwd") if payload else "") or ""
        proj, clean_path = normalize_project(eff_cwd)
        if clean_path:
            project_raw_paths[proj].add(clean_path)

        fallback_date = epoch_to_local_date(updated_at or created_at)
        s_in = s_cr = s_cw = s_out = s_reas = s_calls = 0
        mp_toks: dict[tuple[str, str], int] = defaultdict(int)
        mp_max_date: dict[tuple[str, str], str] = {}

        if payload and payload.get("events") is not None:
            prev = [0, 0, 0, 0, 0, 0]
            is_child_or_fork = actor != "main" or bool((payload.get("meta") or {}).get("forked_from_id"))
            for ev in payload["events"]:
                (
                    d_str,
                    m_turn,
                    c_in,
                    c_cr,
                    c_cw,
                    c_out,
                    c_reas,
                    c_tot,
                    l_in,
                    l_cr,
                    l_cw,
                    l_out,
                    l_reas,
                    l_tot,
                    is_comp_base,
                ) = ev[:15]
                ev_ts = ev[15] if len(ev) > 15 else ""
                ev_effort = (ev[16] if len(ev) > 16 else "") or str(_effort or "")
                cum6 = (c_in, c_cr, c_cw, c_out, c_reas, c_tot)

                # Skip inherited parent prefix turns or compaction header baseline records
                if cum6 in seen_cum or (is_child_or_fork and is_comp_base and prev[5] == 0):
                    prev = [c_in, c_cr, c_cw, c_out, c_reas, c_tot]
                    continue

                seen_cum.add(cum6)
                if prev[5] == 0 and l_tot > 0 and c_tot > l_tot:
                    # First genuine turn after an unseen inherited compaction/fork baseline
                    d_in, d_cr, d_cw, d_out, d_reas = l_in, l_cr, l_cw, l_out, l_reas
                elif c_tot > prev[5]:
                    d_in = max(0, c_in - prev[0])
                    d_cr = max(0, c_cr - prev[1])
                    d_cw = max(0, c_cw - prev[2])
                    d_out = max(0, c_out - prev[3])
                    d_reas = max(0, c_reas - prev[4])
                else:
                    # Counter reset after CLI restart
                    d_in = l_in if l_tot > 0 else c_in
                    d_cr = l_cr if l_tot > 0 else c_cr
                    d_cw = l_cw if l_tot > 0 else c_cw
                    d_out = l_out if l_tot > 0 else c_out
                    d_reas = l_reas if l_tot > 0 else c_reas

                prev = [c_in, c_cr, c_cw, c_out, c_reas, c_tot]
                uncached_in = max(0, d_in - d_cr - d_cw)
                out_txt = max(0, d_out - d_reas)
                turn_sum = uncached_in + d_cr + d_cw + out_txt + d_reas
                if turn_sum <= 0:
                    continue

                eff_key = d_str if d_str != "unknown" else fallback_date
                eff_date = hour_key_date(eff_key)
                if eff_date != "unknown":
                    min_date = min(min_date, eff_date)
                    max_date = max(max_date, eff_date)

                model_id, provider = normalize_model_and_provider(m_turn or db_model, model_provider, "Codex")
                key = (eff_key, "Codex", provider, model_id, proj, actor, 0)
                b = cube_acc[key]
                b[0] += 1
                b[1] += uncached_in
                b[2] += d_cr
                b[3] += d_cw
                b[4] += out_txt
                b[5] += d_reas
                tl = timelines[tid]
                tl["parent"] = tl_parent
                tl["calls"].append([to_epoch_ms(ev_ts), model_id, uncached_in, d_cr, d_cw, out_txt, d_reas, 0.0, ev_effort])

                mp = (model_id, provider)
                mp_toks[mp] += turn_sum
                prev_d = mp_max_date.get(mp)
                if prev_d is None or (eff_date != "unknown" and (prev_d == "unknown" or eff_date > prev_d)):
                    mp_max_date[mp] = eff_date

                s_calls += 1
                s_in += uncached_in
                s_cr += d_cr
                s_cw += d_cw
                s_out += out_txt
                s_reas += d_reas
                total_turns += 1
        elif payload is None and tokens_used and tokens_used > 0 and actor == "main":
            model_id, provider = normalize_model_and_provider(db_model, model_provider, "Codex")
            if fallback_date != "unknown":
                min_date = min(min_date, fallback_date)
                max_date = max(max_date, fallback_date)
            key = (fallback_date, "Codex", provider, model_id, proj, actor, 0)
            b = cube_acc[key]
            b[0] += 1
            b[1] += int(tokens_used)
            mp_toks[(model_id, provider)] += int(tokens_used)
            mp_max_date[(model_id, provider)] = fallback_date
            s_calls = 1
            s_in = int(tokens_used)
            total_turns += 1

        if (s_in + s_cr + s_cw + s_out + s_reas) > 0:
            dom_model, dom_provider = max(mp_toks.items(), key=lambda kv: kv[1])[0]
            dom_date = mp_max_date.get((dom_model, dom_provider), fallback_date)
            clean_title = (title or (payload.get("prompt") if payload else "") or "").strip().replace("\n", " ")[:120]
            if not clean_title:
                clean_title = f"Codex {agent_role or actor} {tid[:8]}"
            sessions_out.append(
                {
                    "id": tid,
                    "harness": "Codex",
                    "title": clean_title,
                    "project": proj,
                    "model": dom_model,
                    "provider": dom_provider,
                    "actor": actor,
                    "date": dom_date,
                    "calls": s_calls,
                    "input_uncached": s_in,
                    "cache_read": s_cr,
                    "cache_write": s_cw,
                    "output_text": s_out,
                    "reasoning": s_reas,
                    "cost_usd": 0.0,
                }
            )

    # Reconstruct exact Codex subscription plan timeline & credit wallet ledger from non-forked rollouts
    USD_PER_CODEX_CREDIT = 0.04  # OpenAI Codex credit packs: 500 credits = $20.00 ($0.04/credit)
    CODEX_PLAN_MONTHLY_USD = {"plus": 20.0, "prolite": 100.0, "pro": 200.0, "team": 30.0}

    main_rl_samples: list[list[Any]] = []
    seen_rl_keys: set[tuple[Any, Any, Any]] = set()
    for fpath in sorted(set(thread_files.values())):
        pl_rl = file_payloads.get(fpath)
        if not pl_rl:
            continue
        mi = pl_rl.get("meta") or {}
        src_s = str(mi.get("source") or "").lower()
        tsrc_s = str(mi.get("thread_source") or "").lower()
        if (
            mi.get("forked_from_id")
            or mi.get("parent_thread_id")
            or tsrc_s in ("subagent", "guardian_review")
            or "subagent" in src_s
            or "guardian" in src_s
        ):
            continue
        for s_row in pl_rl.get("rl_samples") or []:
            rl_key = (s_row[0], s_row[1], s_row[2])
            if rl_key not in seen_rl_keys:
                seen_rl_keys.add(rl_key)
                main_rl_samples.append(s_row)

    main_rl_samples.sort(key=lambda r: r[0])

    plan_day_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    credit_purchases: list[dict[str, Any]] = []
    burns_by_day_usd: dict[str, float] = defaultdict(float)
    burns_by_model_usd: dict[str, float] = defaultdict(float)
    burns_by_project_usd: dict[str, float] = defaultdict(float)
    burns_by_cell_usd: dict[str, float] = defaultdict(float)

    prev_bal: float | None = None
    first_bal_date = ""
    last_bal_date = ""

    def _record_burn(d_loc: str, m_raw: str, c_raw: str, cr_amt: float) -> None:
        usd = cr_amt * USD_PER_CODEX_CREDIT
        p_norm, _ = normalize_project(c_raw)
        m_norm, _ = normalize_model_and_provider(m_raw or "gpt-codex", "openai", "Codex")
        burns_by_day_usd[d_loc] += usd
        burns_by_model_usd[m_norm] += usd
        burns_by_project_usd[p_norm] += usd
        burns_by_cell_usd[f"{d_loc}|{p_norm}|{m_norm}"] += usd

    for ts_iso, bal, pt, p_pct, m_raw, c_raw in main_rl_samples:
        d_loc = iso_to_local_date(ts_iso)
        if pt and d_loc != "unknown":
            plan_day_counts[d_loc][pt.lower()] += 1
        if bal is None:
            continue
        if not first_bal_date and d_loc != "unknown":
            first_bal_date = d_loc
        if d_loc != "unknown":
            last_bal_date = d_loc
        if prev_bal is not None:
            diff = bal - prev_bal
            if diff > 250.0:
                pack_cr = round(diff / 500.0) * 500.0
                sim_burn = max(0.0, pack_cr - diff)
                credit_purchases.append(
                    {
                        "timestamp": ts_iso,
                        "date": d_loc,
                        "credits": round(pack_cr, 2),
                        "usd": round(pack_cr * USD_PER_CODEX_CREDIT, 2),
                        "kind": "initial" if prev_bal < 1.0 else "auto_reload",
                        "balance_before": round(prev_bal, 2),
                        "balance_after": round(bal, 2),
                        "primary_quota_pct": round(p_pct, 1),
                    }
                )
                if sim_burn > 1e-6:
                    _record_burn(d_loc, m_raw, c_raw, sim_burn)
            elif -250.0 < diff < -1e-6:
                _record_burn(d_loc, m_raw, c_raw, -diff)
        prev_bal = bal

    plan_segments: list[dict[str, Any]] = []
    for d_loc in sorted(plan_day_counts.keys()):
        dom_pt = max(plan_day_counts[d_loc].items(), key=lambda kv: kv[1])[0]
        if not plan_segments or plan_segments[-1]["plan_type"] != dom_pt:
            plan_segments.append(
                {
                    "plan_type": dom_pt,
                    "start_date": d_loc,
                    "end_date": d_loc,
                    "monthly_usd": CODEX_PLAN_MONTHLY_USD.get(dom_pt, 0.0),
                }
            )
        else:
            plan_segments[-1]["end_date"] = d_loc

    for seg in plan_segments:
        try:
            sd = dt.date.fromisoformat(seg["start_date"])
            ed = dt.date.fromisoformat(seg["end_date"])
            # Count monthly renewal cycles started inside [sd .. ed]
            cycles = 0
            cur_y, cur_m = sd.year, sd.month
            while True:
                last_dom = (dt.date(cur_y + (1 if cur_m == 12 else 0), 1 if cur_m == 12 else cur_m + 1, 1) - dt.timedelta(days=1)).day
                cyc_date = dt.date(cur_y, cur_m, min(sd.day, last_dom))
                if cyc_date > ed:
                    break
                if cyc_date >= sd:
                    cycles += 1
                if cur_m == 12:
                    cur_y += 1
                    cur_m = 1
                else:
                    cur_m += 1
            seg["cycles"] = max(1, cycles)
            seg["estimated_usd"] = round(seg["cycles"] * seg["monthly_usd"], 2)
        except ValueError:
            seg["cycles"] = 1
            seg["estimated_usd"] = seg["monthly_usd"]

    tot_purch_cr = round(sum(p["credits"] for p in credit_purchases), 4)
    rem_cr = round(prev_bal or 0.0, 4)
    tot_burn_cr = round(max(0.0, tot_purch_cr - rem_cr), 4)

    codex_wallet = {
        "usd_per_credit": USD_PER_CODEX_CREDIT,
        "plan_segments": plan_segments,
        "purchases": credit_purchases,
        "credits_purchased": round(tot_purch_cr, 2),
        "credits_burned": round(tot_burn_cr, 2),
        "credits_remaining": round(rem_cr, 2),
        "overage_billed_usd": round(tot_purch_cr * USD_PER_CODEX_CREDIT, 2),
        "overage_burned_usd": round(tot_burn_cr * USD_PER_CODEX_CREDIT, 2),
        "overage_remaining_usd": round(rem_cr * USD_PER_CODEX_CREDIT, 2),
        "burns_by_day_usd": {k: round(v, 4) for k, v in sorted(burns_by_day_usd.items())},
        "burns_by_model_usd": {k: round(v, 4) for k, v in sorted(burns_by_model_usd.items(), key=lambda x: -x[1])},
        "burns_by_project_usd": {k: round(v, 4) for k, v in sorted(burns_by_project_usd.items(), key=lambda x: -x[1])},
        "burns_by_cell_usd": {k: round(v, 6) for k, v in burns_by_cell_usd.items()},
        "first_sample_date": first_bal_date,
        "last_sample_date": last_bal_date,
    }

    elapsed_ms = round((time.time() - t0) * 1000, 1)
    return {
        "cube": cube_acc,
        "sessions": sessions_out,
        "timelines": timelines,
        "project_paths": project_raw_paths,
        "codex_wallet": codex_wallet,
        "health": {
            "harness": "Codex",
            "source_path": "~/.codex/state_5.sqlite + ~/.codex/sessions/*.jsonl.zst",
            "files_total": len(active_paths),
            "cache_hits": cache_hits,
            "files_parsed": len(to_parse_map),
            "sessions_count": len(sessions_out),
            "api_calls": total_turns,
            "live_date_range": f"{min_date} .. {max_date}" if sessions_out else "n/a",
            "elapsed_ms": elapsed_ms,
        },
    }


# ---------------------------------------------------------------------------
# 3. OpenCode Parser (Session-Signature Incremental Sync — Streaming-Safe)
# ---------------------------------------------------------------------------


def extract_opencode(cache_con: sqlite3.Connection) -> dict[str, Any]:
    """
    Extract OpenCode usage from ~/.local/share/opencode/opencode.db.
    Tracks per-session `(time_updated, tokens_*)` signatures from `session_v2` (~1ms)
    and re-syncs only changed/new sessions via indexed `session_id` lookups on
    `message` and `session_message` — capturing completed streaming updates, v1->v2
    hybrid continuations, and pruning deleted/compacted rows in <5ms warm!
    """
    t0 = time.time()
    db_path = HOME / ".local/share/opencode/opencode.db"
    if not db_path.exists():
        return {"cube": {}, "sessions": [], "project_paths": {}, "health": {"harness": "OpenCode", "files_total": 0}}

    cache_con.executescript(
        """
        CREATE TABLE IF NOT EXISTS opencode_sess_state (
            session_id TEXT PRIMARY KEY,
            sig TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS opencode_msg_cache (
            msg_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            date_str TEXT NOT NULL,
            model_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            inp INTEGER NOT NULL,
            cr INTEGER NOT NULL,
            cw INTEGER NOT NULL,
            out_txt INTEGER NOT NULL,
            reas INTEGER NOT NULL,
            cost REAL NOT NULL,
            ts_ms INTEGER NOT NULL DEFAULT 0,
            variant TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS opencode_msg_cache_sess_idx ON opencode_msg_cache(session_id);
        """
    )

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()

    sv2_rows = cur.execute(
        """
        SELECT id, parent_id, directory, title, agent, model,
               cost, tokens_input, tokens_output, tokens_reasoning,
               tokens_cache_read, tokens_cache_write, time_created, time_updated
        FROM session_v2
        """
    ).fetchall()

    cached_sigs = {
        sid: sig for sid, sig in cache_con.execute("SELECT session_id, sig FROM opencode_sess_state")
    }

    sess_meta: dict[str, dict[str, Any]] = {}
    project_raw_paths: dict[str, set[str]] = defaultdict(set)
    dirty_sids: list[tuple[str, str]] = []
    live_sids: set[str] = set()

    for row in sv2_rows:
        (
            sid,
            parent_id,
            directory,
            title,
            agent,
            model_json,
            cost,
            t_in,
            t_out,
            t_reas,
            t_cr,
            t_cw,
            t_created,
            t_updated,
        ) = row
        live_sids.add(sid)
        sig = f"{t_updated}:{t_in}:{t_out}:{t_reas}:{t_cr}:{t_cw}:{cost}"
        if cached_sigs.get(sid) != sig:
            dirty_sids.append((sid, sig))

        proj, clean_path = normalize_project(directory)
        if clean_path:
            project_raw_paths[proj].add(clean_path)
        m_id, p_id, m_var = "", "", ""
        if model_json:
            try:
                mo = json.loads(model_json)
                m_id = mo.get("id") or ""
                p_id = mo.get("providerID") or ""
                m_var = str(mo.get("variant") or "").lower()
            except Exception:
                m_id = model_json
        actor = "subagent" if parent_id else "main"
        if agent and "review" in agent.lower() and parent_id:
            actor = "auto_review"
        sess_meta[sid] = {
            "parent_id": parent_id,
            "proj": proj,
            "title": (title or f"OpenCode {agent or 'session'} {sid[:10]}").strip().replace("\n", " ")[:120],
            "agent": agent or "",
            "m_id": m_id,
            "p_id": p_id,
            "m_var": m_var,
            "actor": actor,
            "sv2_totals": (
                int(t_in or 0),
                int(t_cr or 0),
                int(t_cw or 0),
                int(t_out or 0),
                int(t_reas or 0),
                float(cost or 0.0),
            ),
            "date": epoch_to_local_date(t_updated or t_created),
        }

    deleted_sids = set(cached_sigs.keys()) - live_sids
    synced_msgs = 0

    if deleted_sids:
        del_params = [(s,) for s in deleted_sids]
        cache_con.executemany("DELETE FROM opencode_msg_cache WHERE session_id = ?", del_params)
        cache_con.executemany("DELETE FROM opencode_sess_state WHERE session_id = ?", del_params)

    if dirty_sids:
        msg_inserts: list[tuple[Any, ...]] = []
        dirty_only = [(s,) for s, _ in dirty_sids]
        cache_con.executemany("DELETE FROM opencode_msg_cache WHERE session_id = ?", dirty_only)

        for sid, _sig in dirty_sids:
            seen_mids: set[str] = set()
            # 1. Legacy v1 `message` rows for this session (indexed by session_id)
            for mid, t_created, data_str in cur.execute(
                "SELECT id, time_created, data FROM message WHERE session_id = ?",
                (sid,),
            ):
                seen_mids.add(mid)
                if '"tokens"' not in data_str:
                    continue
                try:
                    o = json.loads(data_str)
                except Exception:
                    continue
                if o.get("role") != "assistant":
                    continue
                tok = o.get("tokens") or {}
                inp = int(tok.get("input") or 0)
                out_txt = int(tok.get("output") or 0)
                reas = int(tok.get("reasoning") or 0)
                cache_obj = tok.get("cache") or {}
                cr = int(cache_obj.get("read") or 0)
                cw = int(cache_obj.get("write") or 0)
                c_usd = float(o.get("cost") or 0.0)
                if inp + out_txt + reas + cr + cw == 0 and c_usd == 0:
                    continue
                msg_inserts.append(
                    (
                        mid,
                        sid,
                        epoch_to_local_hour_key(t_created),
                        str(o.get("modelID") or ""),
                        str(o.get("providerID") or ""),
                        inp,
                        cr,
                        cw,
                        out_txt,
                        reas,
                        c_usd,
                        to_epoch_ms(t_created),
                        str(o.get("variant") or "").lower(),
                    )
                )

            # 2. v2 `session_message` assistant rows for this session (indexed by (session_id, type, seq)),
            #    including any v2 continuations on legacy v1 sessions!
            for mid, t_created, m_id, p_id, m_var, inp, cr, cw, out_txt, reas, c_usd in cur.execute(
                """
                SELECT id, time_created,
                       json_extract(data, '$.model.id'),
                       json_extract(data, '$.model.providerID'),
                       json_extract(data, '$.model.variant'),
                       json_extract(data, '$.tokens.input'),
                       json_extract(data, '$.tokens.cache.read'),
                       json_extract(data, '$.tokens.cache.write'),
                       json_extract(data, '$.tokens.output'),
                       json_extract(data, '$.tokens.reasoning'),
                       json_extract(data, '$.cost')
                FROM session_message
                WHERE session_id = ? AND type = 'assistant'
                """,
                (sid,),
            ):
                if mid in seen_mids:
                    continue
                inp_i = int(inp or 0)
                cr_i = int(cr or 0)
                cw_i = int(cw or 0)
                out_i = int(out_txt or 0)
                reas_i = int(reas or 0)
                cost_f = float(c_usd or 0.0)
                if inp_i + cr_i + cw_i + out_i + reas_i == 0 and cost_f == 0:
                    continue
                msg_inserts.append(
                    (
                        mid,
                        sid,
                        epoch_to_local_hour_key(t_created),
                        str(m_id or ""),
                        str(p_id or ""),
                        inp_i,
                        cr_i,
                        cw_i,
                        out_i,
                        reas_i,
                        cost_f,
                        to_epoch_ms(t_created),
                        str(m_var or "").lower(),
                    )
                )

        if msg_inserts:
            cache_con.executemany(
                """
                INSERT OR REPLACE INTO opencode_msg_cache
                (msg_id, session_id, date_str, model_id, provider_id, inp, cr, cw, out_txt, reas, cost, ts_ms, variant)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                msg_inserts,
            )
            synced_msgs = len(msg_inserts)
        cache_con.executemany(
            "INSERT OR REPLACE INTO opencode_sess_state(session_id, sig) VALUES (?, ?)",
            dirty_sids,
        )

    con.close()
    if deleted_sids or dirty_sids:
        cache_con.commit()

    cube_acc: dict[tuple[str, str, str, str, str, str, int], list[float]] = defaultdict(
        lambda: [0, 0, 0, 0, 0, 0, 0.0]
    )
    sess_msg_acc: dict[str, dict[str, Any]] = {}
    min_date, max_date = "9999-99-99", "0000-00-00"
    total_msgs = 0

    promo_provs = {str(x) for x in (load_local_config().get("opencode_promo_providers") or [])}
    promo_anthropic_usd = 0.0
    non_promo_logged_usd = 0.0
    copilot_routed_usd = 0.0
    promo_min_date, promo_max_date = "9999-99-99", "0000-00-00"
    paid_min_date, paid_max_date = "9999-99-99", "0000-00-00"

    def _classify_oc_cost(raw_c: float, is_cop_routed: bool, prov_label: str, d_iso: str) -> None:
        nonlocal promo_anthropic_usd, non_promo_logged_usd, copilot_routed_usd
        nonlocal promo_min_date, promo_max_date, paid_min_date, paid_max_date
        if raw_c <= 0:
            return
        if is_cop_routed:
            copilot_routed_usd += raw_c
        elif prov_label in promo_provs:
            promo_anthropic_usd += raw_c
            if d_iso != "unknown":
                promo_min_date = min(promo_min_date, d_iso)
                promo_max_date = max(promo_max_date, d_iso)
        else:
            non_promo_logged_usd += raw_c
            if d_iso != "unknown":
                paid_min_date = min(paid_min_date, d_iso)
                paid_max_date = max(paid_max_date, d_iso)

    timelines = new_timelines()
    for sid, d_str, m_id, p_id, inp, cr, cw, out_txt, reas, cost, ts_ms, m_var in cache_con.execute(
        "SELECT session_id, date_str, model_id, provider_id, inp, cr, cw, out_txt, reas, cost, ts_ms, variant FROM opencode_msg_cache"
    ):
        meta = sess_meta.get(sid)
        if meta is None:
            continue
        proj = meta["proj"]
        actor = meta["actor"]
        eff_m = m_id or meta["m_id"]
        eff_p = p_id or meta["p_id"]
        eff_var = str(m_var or meta.get("m_var") or "")
        is_sub_routed = (eff_p or "").strip().lower() in ("github-copilot", "copilot")
        raw_cost = float(cost or 0.0)
        model, provider = normalize_model_and_provider(eff_m, eff_p, "OpenCode")
        eff_key = d_str if d_str != "unknown" else meta["date"]
        eff_date = hour_key_date(eff_key)
        _classify_oc_cost(raw_cost, is_sub_routed, provider, eff_date)

        if eff_date != "unknown":
            min_date = min(min_date, eff_date)
            max_date = max(max_date, eff_date)

        key = (eff_key, "OpenCode", provider, model, proj, actor, 0)
        tl = timelines[sid]
        tl["parent"] = meta.get("parent_id") or ""
        tl["calls"].append([int(ts_ms or 0), model, inp, cr, cw, out_txt, reas, raw_cost, eff_var])
        b = cube_acc[key]
        b[0] += 1
        b[1] += inp
        b[2] += cr
        b[3] += cw
        b[4] += out_txt
        b[5] += reas
        b[6] += raw_cost

        sa = sess_msg_acc.get(sid)
        if sa is None:
            sa = {
                "calls": 0,
                "inp": 0,
                "cr": 0,
                "cw": 0,
                "out": 0,
                "reas": 0,
                "cost": 0.0,
                "mp_toks": defaultdict(int),
                "mp_max_date": {},
            }
            sess_msg_acc[sid] = sa
        sa["calls"] += 1
        sa["inp"] += inp
        sa["cr"] += cr
        sa["cw"] += cw
        sa["out"] += out_txt
        sa["reas"] += reas
        sa["cost"] += raw_cost
        mp = (model, provider)
        sa["mp_toks"][mp] += inp + cr + cw + out_txt + reas
        prev_d = sa["mp_max_date"].get(mp)
        if prev_d is None or (eff_date != "unknown" and (prev_d == "unknown" or eff_date > prev_d)):
            sa["mp_max_date"][mp] = eff_date
        total_msgs += 1

    sessions_out: list[dict[str, Any]] = []
    for sid, meta in sess_meta.items():
        sa = sess_msg_acc.get(sid)
        if sa is not None:
            dom_model, dom_provider = max(sa["mp_toks"].items(), key=lambda kv: kv[1])[0]
            dom_date = sa["mp_max_date"].get((dom_model, dom_provider), meta["date"])
            sessions_out.append(
                {
                    "id": sid,
                    "harness": "OpenCode",
                    "title": meta["title"],
                    "project": meta["proj"],
                    "model": dom_model,
                    "provider": dom_provider,
                    "actor": meta["actor"],
                    "date": dom_date,
                    "calls": sa["calls"],
                    "input_uncached": sa["inp"],
                    "cache_read": sa["cr"],
                    "cache_write": sa["cw"],
                    "output_text": sa["out"],
                    "reasoning": sa["reas"],
                    "cost_usd": round(sa["cost"], 6),
                }
            )
        else:
            # Fallback only for compacted sessions with 0 live assistant rows
            t_in, t_cr, t_cw, t_out, t_reas, cost = meta["sv2_totals"]
            raw_cost = float(cost or 0.0)
            is_sub_routed = (meta["p_id"] or "").strip().lower() in ("github-copilot", "copilot")
            if (t_in + t_cr + t_cw + t_out + t_reas) == 0 and raw_cost == 0:
                continue
            model, provider = normalize_model_and_provider(meta["m_id"], meta["p_id"], "OpenCode")
            d_str = meta["date"]
            _classify_oc_cost(raw_cost, is_sub_routed, provider, d_str)
            if d_str != "unknown":
                min_date = min(min_date, d_str)
                max_date = max(max_date, d_str)
            key = (d_str, "OpenCode", provider, model, meta["proj"], meta["actor"], 0)
            b = cube_acc[key]
            b[0] += 1
            b[1] += t_in
            b[2] += t_cr
            b[3] += t_cw
            b[4] += t_out
            b[5] += t_reas
            b[6] += raw_cost
            total_msgs += 1
            sessions_out.append(
                {
                    "id": sid,
                    "harness": "OpenCode",
                    "title": meta["title"],
                    "project": meta["proj"],
                    "model": model,
                    "provider": provider,
                    "actor": meta["actor"],
                    "date": d_str,
                    "calls": 1,
                    "input_uncached": t_in,
                    "cache_read": t_cr,
                    "cache_write": t_cw,
                    "output_text": t_out,
                    "reasoning": t_reas,
                    "cost_usd": round(raw_cost, 6),
                }
            )

    sv2_header_cost = (
        sum(
            float(m["sv2_totals"][5] or 0.0)
            for m in sess_meta.values()
            if (m["p_id"] or "").strip().lower() not in ("github-copilot", "copilot")
            and normalize_model_and_provider(m["m_id"], m["p_id"], "OpenCode")[1] in promo_provs
        )
        if promo_provs
        else 0.0
    )

    elapsed_ms = round((time.time() - t0) * 1000, 1)
    return {
        "cube": cube_acc,
        "sessions": sessions_out,
        "timelines": timelines,
        "project_paths": project_raw_paths,
        "opencode_billing": {
            "promo_providers": sorted(promo_provs),
            "promo_anthropic_usd": round(promo_anthropic_usd, 2),
            "session_header_promo_usd": round(sv2_header_cost, 2),
            "direct_paid_usd": round(non_promo_logged_usd, 2),
            "copilot_routed_usd": round(copilot_routed_usd, 2),
            "promo_start_date": promo_min_date if promo_min_date <= promo_max_date else "",
            "promo_end_date": promo_max_date if promo_min_date <= promo_max_date else "",
            "paid_start_date": paid_min_date if paid_min_date <= paid_max_date else "",
            "paid_end_date": paid_max_date if paid_min_date <= paid_max_date else "",
        },
        "health": {
            "harness": "OpenCode",
            "source_path": "~/.local/share/opencode/opencode.db",
            "files_total": len(sv2_rows),
            "cache_hits": len(sv2_rows) - len(dirty_sids),
            "files_parsed": synced_msgs,
            "sessions_count": len(sessions_out),
            "api_calls": total_msgs,
            "live_date_range": f"{min_date} .. {max_date}" if sessions_out else "n/a",
            "elapsed_ms": elapsed_ms,
        },
    }


# ---------------------------------------------------------------------------
# 4. Antigravity Parser (Binary Trajectory SQLite DBs + Hardened JSONL Fallback)
# ---------------------------------------------------------------------------

MODEL_CHANGE_PAIR_RE = re.compile(
    r"The user changed setting `Model Selection` from (.*?) to (.+?)\. No need to comment"
)
USER_REQ_BLOCK_RE = re.compile(r"<USER_REQUEST>.*?</USER_REQUEST>", re.DOTALL)
USER_SETTINGS_BLOCK_RE = re.compile(r"<USER_SETTINGS_CHANGE>(.*?)</USER_SETTINGS_CHANGE>", re.DOTALL)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FILE_URI_RE = re.compile(rb"file:///[^\x00-\x1f\"'<>\\]+")


def _pb_varint(buf: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    n = len(buf)
    while pos < n:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
    return val, pos


def _pb_iter(buf: bytes):
    """Lightweight zero-dependency Protobuf wire-format field iterator."""
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _pb_varint(buf, pos)
        field = tag >> 3
        wtype = tag & 7
        if wtype == 0:
            v, pos = _pb_varint(buf, pos)
            yield field, 0, v
        elif wtype == 1:
            if pos + 8 > n:
                break
            yield field, 1, buf[pos : pos + 8]
            pos += 8
        elif wtype == 2:
            ln, pos = _pb_varint(buf, pos)
            if pos + ln > n:
                break
            yield field, 2, buf[pos : pos + ln]
            pos += ln
        elif wtype == 5:
            if pos + 4 > n:
                break
            yield field, 5, buf[pos : pos + 4]
            pos += 4
        else:
            break


def _parse_antigravity_db(db_path: str) -> dict[str, Any]:
    """
    Worker: decode exact per-generation API model slugs, token counts (including
    Gemini thinking_tokens and pre-Oct-14 Gemini 3.6/3.7 Flash telemetry), step
    timestamps, workspace URIs, and parent references from a trajectory SQLite .db.
    """
    uri = ""
    parent_cid = ""
    prompt = ""
    gens: list[list[Any]] = []  # [[epoch_sec, model_str, inp, cr, cw, out_txt, reas], ...]

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            tmb = con.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1").fetchone()
            if tmb and tmb[0]:
                found_uris = [u.decode("utf-8", "replace") for u in FILE_URI_RE.findall(tmb[0])]
                non_wt = [u for u in found_uris if "/.gemini/antigravity/worktrees/" not in u]
                uri = non_wt[0] if non_wt else (found_uris[0] if found_uris else "")
        except sqlite3.Error:
            pass

        try:
            pr = con.execute("SELECT data FROM parent_references LIMIT 1").fetchone()
            if pr and pr[0]:
                cids = UUID_RE.findall(pr[0].decode("utf-8", "ignore"))
                stem = Path(db_path).stem
                other = [c for c in cids if c != stem]
                parent_cid = other[0] if other else (cids[0] if cids else "")
        except sqlite3.Error:
            pass

        step_ts: dict[int, int] = {}
        first_step_ts = 0
        try:
            for sidx, stype, meta, spayload in con.execute(
                "SELECT idx, step_type, metadata, step_payload FROM steps ORDER BY idx"
            ):
                if meta:
                    for f1, wt1, v1 in _pb_iter(meta):
                        if f1 == 1 and wt1 == 2:
                            for f2, wt2, v2 in _pb_iter(v1):
                                if f2 == 1 and wt2 == 0 and v2 > 1_600_000_000:
                                    step_ts[sidx] = v2
                                    if not first_step_ts:
                                        first_step_ts = v2
                                    break
                            break
                if not prompt and stype == 14 and spayload:
                    txt = spayload.decode("utf-8", "ignore")
                    if "<USER_REQUEST>" in txt:
                        q = txt.split("<USER_REQUEST>", 1)[1].split("</USER_REQUEST>", 1)[0].strip()
                        prompt = q.replace("\n", " ")[:120]
        except sqlite3.Error:
            pass

        try:
            for _gidx, data in con.execute("SELECT idx, data FROM gen_metadata ORDER BY idx"):
                if not data:
                    continue
                step_indices: list[int] = []
                gen_msg = b""
                for f1, wt1, v1 in _pb_iter(data):
                    if f1 == 1 and wt1 == 2:
                        gen_msg = v1
                    elif f1 == 2:
                        if wt1 == 2:
                            p = 0
                            while p < len(v1):
                                sv, p = _pb_varint(v1, p)
                                step_indices.append(sv)
                        elif wt1 == 0:
                            step_indices.append(v1)
                if not gen_msg:
                    continue

                enum_id = 0
                slug = ""
                ui_label = ""
                gen_ts = 0
                last_step_idx = -1
                usage: dict[int, int] = {}

                for f2, wt2, v2 in _pb_iter(gen_msg):
                    if f2 == 3 and wt2 == 0:
                        enum_id = v2
                    elif f2 == 4 and wt2 == 2:
                        for f3, wt3, v3 in _pb_iter(v2):
                            if wt3 == 0:
                                usage[f3] = v3
                    elif f2 == 9 and wt2 == 2:
                        for f3, wt3, v3 in _pb_iter(v2):
                            if f3 == 4 and wt3 == 2:
                                for f4, wt4, v4 in _pb_iter(v3):
                                    if f4 == 1 and wt4 == 0 and v4 > 1_600_000_000:
                                        gen_ts = v4
                    elif f2 == 19 and wt2 == 2:
                        slug = v2.decode("utf-8", "replace").strip()
                    elif f2 == 21 and wt2 == 2:
                        ui_label = v2.decode("utf-8", "replace").strip()
                    elif f2 == 20 and wt2 == 2:
                        k_s = v_s = ""
                        for f3, wt3, v3 in _pb_iter(v2):
                            if f3 == 1 and wt3 == 2:
                                k_s = v3.decode("utf-8", "replace")
                            elif f3 == 2 and wt3 == 2:
                                v_s = v3.decode("utf-8", "replace")
                        if k_s == "last_step_index" and v_s.isdigit():
                            last_step_idx = int(v_s)

                inp = int(usage.get(2, 0))
                out_tot = int(usage.get(3, 0))
                cw = int(usage.get(4, 0))
                cr = int(usage.get(5, 0))
                reas = int(usage.get(9, 0))
                if inp + cr + cw + out_tot == 0:
                    continue
                out_txt = max(0, out_tot - reas)

                if not gen_ts:
                    for si in step_indices:
                        if si in step_ts:
                            gen_ts = step_ts[si]
                            break
                if not gen_ts and last_step_idx in step_ts:
                    gen_ts = step_ts[last_step_idx]
                if not gen_ts:
                    gen_ts = first_step_ts

                model_str = slug or ui_label or AG_ENUM_TO_MODEL.get(enum_id, f"enum-{enum_id}")
                gens.append([gen_ts, model_str, inp, cr, cw, out_txt, reas])
        except sqlite3.Error:
            pass

        con.close()
    except sqlite3.Error:
        pass

    return {
        "source_kind": "db",
        "uri": uri,
        "parent_cid": parent_cid,
        "prompt": prompt,
        "gens": gens,
    }


def _parse_antigravity_transcript(path: str) -> dict[str, Any]:
    """
    Fallback parser used ONLY if a brain/<cid> transcript ever lacks a conversations/<cid>.db.
    Hardened against <USER_REQUEST> text spoofing, pre-switch model attribution, and GENERIC tool output.
    """
    cur_model = ""
    pre_switch_model = ""
    timeline: list[list[str]] = []
    steps: list[list[Any]] = []
    first_user_prompt = ""

    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"USER_INPUT"' in raw and (b"USER_SETTINGS_CHANGE" in raw or not first_user_prompt):
                    try:
                        o = json.loads(raw)
                        if o.get("type") == "USER_INPUT":
                            ts = str(o.get("created_at") or "")
                            content = str(o.get("content") or "")
                            stripped_ur = USER_REQ_BLOCK_RE.sub("", content)
                            for block in USER_SETTINGS_BLOCK_RE.findall(stripped_ur):
                                for frm_m, to_m in MODEL_CHANGE_PAIR_RE.findall(block):
                                    frm_clean = frm_m.strip()
                                    to_clean = to_m.strip()
                                    if not timeline and frm_clean and frm_clean != "None":
                                        pre_switch_model = frm_clean
                                    cur_model = to_clean
                                    timeline.append([ts, cur_model])
                            if not first_user_prompt and "<USER_REQUEST>" in content:
                                q = content.split("<USER_REQUEST>", 1)[1].split("</USER_REQUEST>", 1)[0].strip()
                                first_user_prompt = q.replace("\n", " ")[:120]
                    except Exception:
                        pass
                elif b'"PLANNER_RESPONSE"' in raw:
                    try:
                        o = json.loads(raw)
                        if o.get("type") != "PLANNER_RESPONSE":
                            continue
                        ts = str(o.get("created_at") or "")
                        inp = int(o.get("input_tokens") or 0)
                        cr = int(o.get("cache_read_tokens") or 0)
                        cw = int(o.get("cache_creation_tokens") or 0)
                        out_tot = int(o.get("output_tokens") or 0)
                        reas = int(o.get("thinking_tokens") or 0)
                        if inp + cr + cw + out_tot > 0:
                            steps.append([ts, cur_model or pre_switch_model, inp, cr, cw, max(0, out_tot - reas), reas])
                    except Exception:
                        pass
    except OSError:
        pass

    if pre_switch_model:
        for st in steps:
            if not st[1]:
                st[1] = pre_switch_model

    return {
        "source_kind": "jsonl",
        "uri": "",
        "parent_cid": "",
        "prompt": first_user_prompt,
        "gens": steps,
    }


def extract_antigravity(cache_con: sqlite3.Connection, pool: ProcessPoolExecutor | None) -> dict[str, Any]:
    t0 = time.time()
    ag_dir = HOME / ".gemini/antigravity"
    ide_dir = HOME / ".gemini/antigravity-ide"
    sum_db_path = ag_dir / "conversation_summaries.db"
    brain_dir = ag_dir / "brain"

    conv_meta: dict[str, dict[str, Any]] = {}
    proj_id_to_uri: dict[str, str] = {}

    if sum_db_path.exists():
        con = sqlite3.connect(f"file:{sum_db_path}?mode=ro", uri=True)
        for row in con.execute(
            """
            SELECT conversation_id, title, workspace_uris, project_id,
                   agent_name, parent_conversation_id, last_modified_time
            FROM conversation_summaries
            """
        ):
            cid, title, ws_json, pid, agent_name, parent_cid, last_mod = row
            uri = ""
            if ws_json:
                try:
                    uris = json.loads(ws_json)
                    if uris:
                        uri = uris[0]
                except Exception:
                    pass
            if pid and uri and "/.gemini/antigravity/worktrees/" not in uri:
                proj_id_to_uri[pid] = uri
            conv_meta[cid] = {
                "title": title or "",
                "uri": uri,
                "project_id": pid or "",
                "agent_name": agent_name or "",
                "parent_cid": parent_cid or "",
                "last_mod": str(last_mod or ""),
            }
        con.close()

    # Discover all binary trajectory SQLite DBs (cli + ide), plus fallback JSONL if any lacks a .db
    cid_to_source: dict[str, tuple[str, str]] = {}  # cid -> (kind, path)
    for conv_dir in (ag_dir / "conversations", ide_dir / "conversations"):
        if conv_dir.exists():
            for dbp in sorted(conv_dir.glob("*.db")):
                cid = dbp.stem
                if cid not in cid_to_source:
                    cid_to_source[cid] = ("db", str(dbp))

    if brain_dir.exists():
        for bd in sorted(brain_dir.iterdir()):
            if not bd.is_dir() or bd.name == "tempmediaStorage" or bd.name in cid_to_source:
                continue
            full_p = bd / ".system_generated" / "logs" / "transcript_full.jsonl"
            comp_p = bd / ".system_generated" / "logs" / "transcript.jsonl"
            if full_p.exists():
                cid_to_source[bd.name] = ("jsonl", str(full_p))
            elif comp_p.exists():
                cid_to_source[bd.name] = ("jsonl", str(comp_p))

    cached_rows = {
        path: (mtime_ns, size, payload)
        for path, mtime_ns, size, payload in cache_con.execute(
            "SELECT path, mtime_ns, size, payload_json FROM file_cache WHERE harness='antigravity'"
        )
    }

    to_parse_db: list[tuple[str, str, int, int]] = []
    to_parse_jsonl: list[tuple[str, str, int, int]] = []
    conv_payloads: dict[str, dict[str, Any]] = {}
    active_paths: set[str] = set()
    cache_hits = 0

    for cid, (kind, p) in cid_to_source.items():
        sig = _file_sig(p)
        if sig is None:
            continue
        active_paths.add(p)
        mtime_ns, size = sig
        c = cached_rows.get(p)
        if c and c[0] == mtime_ns and c[1] == size:
            conv_payloads[cid] = json.loads(c[2])
            cache_hits += 1
        elif kind == "db":
            to_parse_db.append((cid, p, mtime_ns, size))
        else:
            to_parse_jsonl.append((cid, p, mtime_ns, size))

    inserts: list[tuple[str, str, int, int, str]] = []
    if to_parse_db:
        db_paths = [x[1] for x in to_parse_db]
        fresh_db = list(pool.map(_parse_antigravity_db, db_paths, chunksize=15)) if pool else [_parse_antigravity_db(p) for p in db_paths]
        for (cid, p, mtime_ns, size), payload in zip(to_parse_db, fresh_db):
            conv_payloads[cid] = payload
            inserts.append(("antigravity", p, mtime_ns, size, json.dumps(payload, separators=(",", ":"))))

    if to_parse_jsonl:
        j_paths = [x[1] for x in to_parse_jsonl]
        fresh_j = list(pool.map(_parse_antigravity_transcript, j_paths, chunksize=15)) if pool else [_parse_antigravity_transcript(p) for p in j_paths]
        for (cid, p, mtime_ns, size), payload in zip(to_parse_jsonl, fresh_j):
            conv_payloads[cid] = payload
            inserts.append(("antigravity", p, mtime_ns, size, json.dumps(payload, separators=(",", ":"))))

    if inserts:
        cache_con.executemany(
            "INSERT OR REPLACE INTO file_cache(harness, path, mtime_ns, size, payload_json) VALUES (?, ?, ?, ?, ?)",
            inserts,
        )
        cache_con.commit()

    _prune_file_cache(cache_con, "antigravity", active_paths, set(cached_rows.keys()))

    def resolve_conv_uri(cid: str, visited: set[str] | None = None) -> str:
        if visited is None:
            visited = set()
        if cid in visited:
            return ""
        visited.add(cid)
        meta = conv_meta.get(cid) or {}
        pl = conv_payloads.get(cid) or {}
        uri = meta.get("uri") or pl.get("uri") or ""
        if uri and "/.gemini/antigravity/worktrees/" not in uri:
            return uri
        parent = meta.get("parent_cid") or pl.get("parent_cid") or ""
        if parent:
            pu = resolve_conv_uri(parent, visited)
            if pu:
                return pu
        pid = meta.get("project_id")
        if pid and pid in proj_id_to_uri:
            return proj_id_to_uri[pid]
        return uri

    cube_acc: dict[tuple[str, str, str, str, str, str, int], list[float]] = defaultdict(
        lambda: [0, 0, 0, 0, 0, 0, 0.0]
    )
    sessions_out: list[dict[str, Any]] = []
    project_raw_paths: dict[str, set[str]] = defaultdict(set)
    timelines = new_timelines()
    total_steps = 0
    min_date, max_date = "9999-99-99", "0000-00-00"

    for cid, payload in conv_payloads.items():
        meta = conv_meta.get(cid) or {}
        is_sub = bool(meta.get("parent_cid") or payload.get("parent_cid"))
        actor = "subagent" if is_sub else "main"
        uri = resolve_conv_uri(cid)
        proj, clean_path = normalize_project(uri)
        if clean_path:
            project_raw_paths[proj].add(clean_path)

        s_calls = s_in = s_cr = s_cw = s_out = s_reas = 0
        fallback_date = iso_to_local_date(meta.get("last_mod"))
        mp_toks: dict[tuple[str, str], int] = defaultdict(int)
        mp_max_date: dict[tuple[str, str], str] = {}
        is_db = payload.get("source_kind") == "db"

        for rec in payload.get("gens") or []:
            ts_val, raw_m, inp, cr, cw, out_txt, reas = rec
            d_key = epoch_to_local_hour_key(ts_val) if is_db else iso_to_local_hour_key(str(ts_val))
            eff_key = d_key if d_key != "unknown" else fallback_date
            eff_date = hour_key_date(eff_key)
            if eff_date != "unknown":
                min_date = min(min_date, eff_date)
                max_date = max(max_date, eff_date)

            model, provider = normalize_model_and_provider(raw_m, None, "Antigravity")
            key = (eff_key, "Antigravity", provider, model, proj, actor, 0)
            tl = timelines[cid]
            tl["parent"] = meta.get("parent_cid") or payload.get("parent_cid") or ""
            tl["calls"].append([to_epoch_ms(ts_val), model, inp, cr, cw, out_txt, reas, 0.0, ""])
            b = cube_acc[key]
            b[0] += 1
            b[1] += inp
            b[2] += cr
            b[3] += cw
            b[4] += out_txt
            b[5] += reas

            s_calls += 1
            s_in += inp
            s_cr += cr
            s_cw += cw
            s_out += out_txt
            s_reas += reas
            tok_sum = inp + cr + cw + out_txt + reas
            mp = (model, provider)
            mp_toks[mp] += tok_sum
            prev_d = mp_max_date.get(mp)
            if prev_d is None or (eff_date != "unknown" and (prev_d == "unknown" or eff_date > prev_d)):
                mp_max_date[mp] = eff_date
            total_steps += 1

        if s_in + s_cr + s_cw + s_out + s_reas > 0:
            dom_model, dom_provider = max(mp_toks.items(), key=lambda kv: kv[1])[0]
            dom_date = mp_max_date.get((dom_model, dom_provider), fallback_date)
            title = (
                meta.get("title")
                or payload.get("prompt")
                or f"Antigravity {meta.get('agent_name') or 'chat'} {cid[:8]}"
            ).strip()
            sessions_out.append(
                {
                    "id": cid,
                    "harness": "Antigravity",
                    "title": title.replace("\n", " ")[:120],
                    "project": proj,
                    "model": dom_model,
                    "provider": dom_provider,
                    "actor": actor,
                    "date": dom_date,
                    "calls": s_calls,
                    "input_uncached": s_in,
                    "cache_read": s_cr,
                    "cache_write": s_cw,
                    "output_text": s_out,
                    "reasoning": s_reas,
                    "cost_usd": 0.0,
                }
            )

    elapsed_ms = round((time.time() - t0) * 1000, 1)
    return {
        "cube": cube_acc,
        "sessions": sessions_out,
        "timelines": timelines,
        "project_paths": project_raw_paths,
        "health": {
            "harness": "Antigravity",
            "source_path": "~/.gemini/antigravity/conversations/*.db (+ antigravity-ide)",
            "files_total": len(active_paths),
            "cache_hits": cache_hits,
            "files_parsed": len(to_parse_db) + len(to_parse_jsonl),
            "sessions_count": len(sessions_out),
            "api_calls": total_steps,
            "live_date_range": f"{min_date} .. {max_date}" if sessions_out else "n/a",
            "elapsed_ms": elapsed_ms,
        },
    }


# ---------------------------------------------------------------------------
# 5. GitHub Copilot Extractor (Copilot CLI SQLite + events.jsonl + VS Code Chat)
# ---------------------------------------------------------------------------


def _parse_copilot_workspace_yaml(yaml_path: Path) -> dict[str, str]:
    """Lightweight stdlib parser for flat key: value metadata in Copilot CLI workspace.yaml."""
    out: dict[str, str] = {}
    try:
        text = yaml_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip()
        val = v.strip()
        if len(val) >= 2 and ((val[0] == "'" and val[-1] == "'") or (val[0] == '"' and val[-1] == '"')):
            val = val[1:-1]
        if key and val and val not in ("null", "~"):
            out[key] = val
    return out


def _distribute_int(total: int, weights: list[int]) -> list[int]:
    """Deterministically distribute `total` into `len(weights)` non-negative integers summing to `total`."""
    n = len(weights)
    if n == 0 or total <= 0:
        return [0] * n
    w_sum = sum(weights)
    if w_sum <= 0:
        q, rem = divmod(total, n)
        return [q + (1 if i < rem else 0) for i in range(n)]
    out: list[int] = []
    acc_w = 0
    prev_alloc = 0
    for w in weights:
        acc_w += w
        cur_alloc = (total * acc_w) // w_sum
        out.append(cur_alloc - prev_alloc)
        prev_alloc = cur_alloc
    return out


def _copilot_uncached_in(raw_in: int, cr: int, cw: int) -> int:
    """
    Compute uncached input tokens from Copilot usage fields.
    Copilot CLI & VS Code report OpenAI-style inclusive `inputTokens` (`raw_in >= cr + cw`);
    if a build/provider reports Anthropic-style exclusive `input_tokens` (`cr + cw > raw_in`),
    preserve `raw_in` as uncached rather than clamping to zero.
    """
    return raw_in if (cr + cw > raw_in) else max(0, raw_in - cr - cw)


def _parse_copilot_cli_events(path_str: str) -> dict[str, Any]:
    """
    Worker: parse ~/.copilot/session-state/<id>/events.jsonl (+ sibling workspace.yaml &
    vscode.requests.metadata.json). Handles multi-process `session.resume` -> `session.shutdown`
    segments, distributing segment `modelMetrics` across `assistant.message` timestamps when
    older Copilot CLI / VS Code Agent builds omit per-call `assistant.usage` events.
    """
    p = Path(path_str)
    ws_meta = _parse_copilot_workspace_yaml(p.parent / "workspace.yaml")
    sid = ws_meta.get("id") or p.parent.name
    parent_sid = ""
    cwd = ws_meta.get("cwd") or ws_meta.get("git_root") or ""
    title = ws_meta.get("summary") or ws_meta.get("name") or ""
    prompt_fallback = ""
    start_time = ws_meta.get("created_at") or ""
    last_ts = ws_meta.get("updated_at") or start_time

    vsc_fallback_model = ""
    vsc_req_meta = p.parent / "vscode.requests.metadata.json"
    if vsc_req_meta.is_file():
        try:
            rm_list = json.loads(vsc_req_meta.read_text(encoding="utf-8", errors="replace"))
            if isinstance(rm_list, list):
                for item in rm_list:
                    if isinstance(item, dict):
                        cand_m = str(item.get("responseModelId") or "").strip()
                        if cand_m and cand_m.lower() != "auto":
                            vsc_fallback_model = cand_m
                            break
        except (OSError, ValueError):
            pass

    cur_model = vsc_fallback_model
    default_effort = ""
    segments: list[dict[str, Any]] = [
        {"calls": [], "msgs": [], "shutdown_metrics": {}, "shutdown_ts": ""}
    ]

    try:
        with open(path_str, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    ev = json.loads(s)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                try:
                    etype = ev.get("type") or ""
                    ts = str(ev.get("timestamp") or "")
                    if ts:
                        last_ts = ts
                    data = ev.get("data")
                    if not isinstance(data, dict):
                        data = {}
                    seg = segments[-1]

                    if etype == "session.start":
                        sid = str(data.get("sessionId") or sid)
                        if data.get("startTime"):
                            start_time = str(data["startTime"])
                        sel_m = str(data.get("selectedModel") or "").strip()
                        if sel_m and sel_m.lower() != "auto":
                            cur_model = sel_m
                        if data.get("reasoningEffort"):
                            default_effort = str(data["reasoningEffort"]).lower()
                        if data.get("detachedFromSpawningParentSessionId"):
                            parent_sid = str(data["detachedFromSpawningParentSessionId"])
                        ctx = data.get("context")
                        if isinstance(ctx, dict) and not cwd:
                            cwd = str(ctx.get("cwd") or ctx.get("gitRoot") or "")
                    elif etype == "session.resume":
                        if seg["calls"] or seg["msgs"] or seg["shutdown_metrics"]:
                            seg = {"calls": [], "msgs": [], "shutdown_metrics": {}, "shutdown_ts": ""}
                            segments.append(seg)
                        sel_m = str(data.get("selectedModel") or "").strip()
                        if sel_m and sel_m.lower() != "auto":
                            cur_model = sel_m
                        ctx = data.get("context")
                        if isinstance(ctx, dict) and not cwd:
                            cwd = str(ctx.get("cwd") or ctx.get("gitRoot") or "")
                    elif etype == "session.context_changed":
                        if not cwd:
                            cwd = str(data.get("cwd") or data.get("gitRoot") or "")
                    elif etype == "session.model_change":
                        new_m = str(data.get("newModel") or "").strip()
                        if new_m and new_m.lower() != "auto":
                            cur_model = new_m
                    elif etype == "session.title_changed":
                        t_val = str(data.get("title") or "").strip()
                        if t_val:
                            title = t_val
                    elif etype == "user.message" and not prompt_fallback:
                        c_val = data.get("content") or data.get("prompt") or ""
                        if isinstance(c_val, str) and c_val.strip():
                            prompt_fallback = c_val.strip()[:120]
                    elif etype == "assistant.message":
                        msg_m = str(data.get("model") or "").strip()
                        if msg_m and msg_m.lower() != "auto":
                            cur_model = msg_m
                        else:
                            msg_m = cur_model
                        out_w = max(0, int(data.get("outputTokens") or 0))
                        eff = str(data.get("reasoningEffort") or default_effort or "").lower()
                        sub_k = str(ev.get("agentId") or data.get("parentToolCallId") or "")
                        seg["msgs"].append([ts or last_ts, msg_m, out_w, eff, sub_k])
                    elif etype == "assistant.usage":
                        m_cand = str(data.get("model") or "").strip()
                        if m_cand.lower() == "auto":
                            m_cand = ""
                        raw_m = m_cand or cur_model or vsc_fallback_model or "(unknown)"
                        cr = max(0, int(data.get("cacheReadTokens") or 0))
                        cw = max(0, int(data.get("cacheWriteTokens") or 0))
                        raw_in = max(0, int(data.get("inputTokens") or 0))
                        inp = _copilot_uncached_in(raw_in, cr, cw)
                        reas = max(0, int(data.get("reasoningTokens") or 0))
                        raw_out = max(0, int(data.get("outputTokens") or 0))
                        out_txt = max(0, raw_out - reas)
                        if inp + cr + cw + out_txt + reas <= 0:
                            continue
                        init = str(data.get("initiator") or "")
                        mult = (
                            float(data["cost"])
                            if (init == "user" and data.get("cost") is not None)
                            else (1.0 if init == "user" else 0.0)
                        )
                        eff = str(data.get("reasoningEffort") or default_effort or "").lower()
                        sub_k = str(ev.get("agentId") or data.get("parentToolCallId") or "") if init == "sub-agent" else ""
                        seg["calls"].append([ts or last_ts, raw_m, inp, cr, cw, out_txt, reas, mult, init, eff, sub_k, 0])
                    elif etype == "session.compaction_complete" and data.get("success") is True:
                        ctu = data.get("compactionTokensUsed")
                        if isinstance(ctu, dict):
                            m_cand = str(ctu.get("model") or "").strip()
                            if m_cand.lower() == "auto":
                                m_cand = ""
                            raw_m = m_cand or cur_model or vsc_fallback_model or "(unknown)"
                            cr = max(0, int(ctu.get("cacheReadTokens") or 0))
                            cw = max(0, int(ctu.get("cacheWriteTokens") or 0))
                            raw_in = max(0, int(ctu.get("inputTokens") or 0))
                            inp = _copilot_uncached_in(raw_in, cr, cw)
                            raw_out = max(0, int(ctu.get("outputTokens") or 0))
                            if inp + cr + cw + raw_out > 0:
                                seg["calls"].append([ts or last_ts, raw_m, inp, cr, cw, raw_out, 0, 0.0, "compaction", "", "", 0])
                    elif etype == "session.shutdown":
                        cur_m_sh = str(data.get("currentModel") or "").strip()
                        if cur_m_sh and cur_m_sh.lower() != "auto":
                            cur_model = cur_m_sh
                        mm = data.get("modelMetrics")
                        if isinstance(mm, dict):
                            snap: dict[str, list[float]] = {}
                            for m_key, m_obj in mm.items():
                                if not isinstance(m_obj, dict):
                                    continue
                                reqs = m_obj.get("requests") if isinstance(m_obj.get("requests"), dict) else {}
                                usg = m_obj.get("usage") if isinstance(m_obj.get("usage"), dict) else {}
                                r_cnt = max(1, int(reqs.get("count") or 0))
                                r_cost = float(reqs.get("cost") or 0.0)
                                cr = max(0, int(usg.get("cacheReadTokens") or 0))
                                cw = max(0, int(usg.get("cacheWriteTokens") or 0))
                                raw_in = max(0, int(usg.get("inputTokens") or 0))
                                inp = _copilot_uncached_in(raw_in, cr, cw)
                                reas = max(0, int(usg.get("reasoningTokens") or 0))
                                raw_out = max(0, int(usg.get("outputTokens") or 0))
                                out_txt = max(0, raw_out - reas)
                                if inp + cr + cw + out_txt + reas > 0:
                                    snap[str(m_key)] = [r_cnt, r_cost, inp, cr, cw, out_txt, reas]
                            if snap:
                                seg["shutdown_metrics"] = snap
                                seg["shutdown_ts"] = ts or last_ts
                except Exception:
                    continue
    except OSError:
        pass

    # Reconcile each process segment (`session.start` / `session.resume` .. `session.shutdown`).
    # Each CLI invocation starts a fresh process whose `session.shutdown` `modelMetrics` reports
    # per-segment `requests.count`, `requests.cost`, and `usage.*` totals for that invocation.
    calls: list[list[Any]] = []
    total_shutdown_metrics: dict[str, list[float]] = {}
    last_shutdown_ts = ""

    for seg in segments:
        seg_calls: list[list[Any]] = list(seg["calls"])
        seg_msgs: list[list[Any]] = seg["msgs"]
        sh_map: dict[str, list[float]] = seg["shutdown_metrics"]
        sh_ts = str(seg["shutdown_ts"] or last_ts)
        if sh_map:
            last_shutdown_ts = sh_ts

        for rm, vals in sh_map.items():
            r_cnt, r_cost, m_inp, m_cr, m_cw, m_out, m_reas = vals
            tot_m = total_shutdown_metrics.setdefault(rm, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            tot_m[0] += r_cnt
            tot_m[1] += r_cost
            tot_m[2] += m_inp
            tot_m[3] += m_cr
            tot_m[4] += m_cw
            tot_m[5] += m_out
            tot_m[6] += m_reas

        if seg_calls:
            if sh_map:
                rec_by_m: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
                for c in seg_calls:
                    rm_c = str(c[1])
                    rm_acc = rec_by_m[rm_c]
                    rm_acc[0] += float(c[7] or 0.0)
                    rm_acc[1] += c[2]
                    rm_acc[2] += c[3]
                    rm_acc[3] += c[4]
                    rm_acc[4] += c[5]
                    rm_acc[5] += c[6]
                for rm, vals in sh_map.items():
                    _rc, r_cost, m_inp, m_cr, m_cw, m_out, m_reas = vals
                    got = rec_by_m.get(rm) or [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
                    d_inp = max(0, int(m_inp - got[1]))
                    d_cr = max(0, int(m_cr - got[2]))
                    d_cw = max(0, int(m_cw - got[3]))
                    d_out = max(0, int(m_out - got[4]))
                    d_reas = max(0, int(m_reas - got[5]))
                    d_mult = max(0.0, float(r_cost) - got[0])
                    if d_inp + d_cr + d_cw + d_out + d_reas > 0:
                        seg_calls.append([sh_ts, rm, d_inp, d_cr, d_cw, d_out, d_reas, d_mult, "user", default_effort, "", 0])
            calls.extend(seg_calls)
        elif sh_map:
            single_model = len(sh_map) == 1
            for rm, vals in sh_map.items():
                _rc, r_cost, m_inp, m_cr, m_cw, m_out, m_reas = vals
                eff_c = float(r_cost)
                matched = [
                    m for m in seg_msgs if m[1] == rm or (single_model and (not m[1] or m[1] == vsc_fallback_model))
                ]
                if matched:
                    n_m = len(matched)
                    ctx_weights = [n_m + idx for idx in range(n_m)]
                    out_weights = [max(1, int(m[2] or 0)) for m in matched]
                    alloc_inp = _distribute_int(int(m_inp), ctx_weights)
                    alloc_cr = _distribute_int(int(m_cr), ctx_weights)
                    alloc_cw = _distribute_int(int(m_cw), ctx_weights)
                    alloc_out = _distribute_int(int(m_out), out_weights)
                    alloc_reas = _distribute_int(int(m_reas), out_weights)
                    for idx, m_ev in enumerate(matched):
                        m_ts, _mm, _ow, m_eff, m_sub = m_ev
                        if alloc_inp[idx] + alloc_cr[idx] + alloc_cw[idx] + alloc_out[idx] + alloc_reas[idx] > 0:
                            calls.append(
                                [
                                    m_ts or sh_ts,
                                    rm,
                                    alloc_inp[idx],
                                    alloc_cr[idx],
                                    alloc_cw[idx],
                                    alloc_out[idx],
                                    alloc_reas[idx],
                                    eff_c if idx == 0 else 0.0,
                                    "user" if idx == 0 else "agent",
                                    m_eff or default_effort,
                                    m_sub,
                                    0,
                                ]
                            )
                else:
                    calls.append(
                        [
                            sh_ts,
                            rm,
                            int(m_inp),
                            int(m_cr),
                            int(m_cw),
                            int(m_out),
                            int(m_reas),
                            eff_c,
                            "user",
                            default_effort,
                            "",
                            0,
                        ]
                    )

    return {
        "sid": sid,
        "parent_sid": parent_sid,
        "cwd": cwd,
        "title": title or prompt_fallback,
        "start_time": start_time,
        "last_ts": last_ts,
        "default_model": cur_model,
        "default_effort": default_effort,
        "calls": calls,
        "shutdown_metrics": total_shutdown_metrics,
        "shutdown_ts": last_shutdown_ts,
        "segments_reconciled": True,
    }


def _parse_copilot_store_db(db_path: str) -> dict[str, Any]:
    """
    Read ~/.copilot/state/session-store.db (read-only), extracting `sessions`, first prompt
    from `turns`, and per-call usage rows from `assistant_usage_events`.
    """
    sessions_meta: dict[str, dict[str, str]] = {}
    calls_by_sid: dict[str, list[list[Any]]] = defaultdict(list)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return {"sessions": {}, "calls": {}}

    try:
        tables = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "sessions" in tables:
            s_cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)").fetchall()}
            sel_cols = [
                c
                for c in ("id", "cwd", "repository", "summary", "agent_name", "created_at", "updated_at")
                if c in s_cols
            ]
            if "id" in sel_cols:
                for row in con.execute(f"SELECT {', '.join(sel_cols)} FROM sessions"):
                    rec = dict(zip(sel_cols, row))
                    sid = str(rec.get("id") or "")
                    if sid:
                        sessions_meta[sid] = {
                            "cwd": str(rec.get("cwd") or ""),
                            "repository": str(rec.get("repository") or ""),
                            "summary": str(rec.get("summary") or ""),
                            "agent_name": str(rec.get("agent_name") or ""),
                            "created_at": str(rec.get("created_at") or ""),
                            "updated_at": str(rec.get("updated_at") or ""),
                        }

        if "turns" in tables:
            t_cols = {r[1] for r in con.execute("PRAGMA table_info(turns)").fetchall()}
            if {"session_id", "user_message"}.issubset(t_cols):
                order_clause = "ORDER BY session_id, turn_index ASC" if "turn_index" in t_cols else ""
                seen_turn_sid: set[str] = set()
                for sid_raw, u_msg in con.execute(
                    f"SELECT session_id, user_message FROM turns WHERE user_message IS NOT NULL AND user_message != '' {order_clause}"
                ):
                    sid = str(sid_raw or "")
                    if sid and sid not in seen_turn_sid:
                        seen_turn_sid.add(sid)
                        m_entry = sessions_meta.setdefault(sid, {})
                        if not m_entry.get("summary"):
                            m_entry["prompt"] = str(u_msg).strip()[:120]

        if "assistant_usage_events" in tables:
            u_cols = {r[1] for r in con.execute("PRAGMA table_info(assistant_usage_events)").fetchall()}
            wanted = [
                "session_id",
                "agent_id",
                "parent_tool_call_id",
                "model",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "request_multiplier",
                "initiator",
                "reasoning_effort",
                "created_at",
            ]
            exprs = [c if c in u_cols else f"NULL AS {c}" for c in wanted]
            order_by = "ORDER BY id ASC" if "id" in u_cols else ""
            for row in con.execute(f"SELECT {', '.join(exprs)} FROM assistant_usage_events {order_by}"):
                try:
                    (
                        sid_raw,
                        agent_id,
                        parent_tc,
                        raw_m,
                        in_tok,
                        out_tok,
                        cr_tok,
                        cw_tok,
                        reas_tok,
                        req_mult,
                        initiator,
                        reas_eff,
                        created_at,
                    ) = row
                    sid = str(sid_raw or "")
                    if not sid:
                        continue
                    cr = max(0, int(cr_tok or 0))
                    cw = max(0, int(cw_tok or 0))
                    raw_in = max(0, int(in_tok or 0))
                    inp = _copilot_uncached_in(raw_in, cr, cw)
                    reas = max(0, int(reas_tok or 0))
                    raw_out = max(0, int(out_tok or 0))
                    out_txt = max(0, raw_out - reas)
                    if inp + cr + cw + out_txt + reas <= 0:
                        continue
                    init = str(initiator or "")
                    mult = (
                        float(req_mult)
                        if (init == "user" and req_mult is not None)
                        else (1.0 if init == "user" else 0.0)
                    )
                    eff = str(reas_eff or "").lower()
                    sub_k = ""
                    if init == "sub-agent" or parent_tc:
                        sub_k = str(agent_id or parent_tc or "subagent")
                    calls_by_sid[sid].append(
                        [
                            created_at if isinstance(created_at, (int, float)) else str(created_at or ""),
                            str(raw_m or "(unknown)"),
                            inp,
                            cr,
                            cw,
                            out_txt,
                            reas,
                            mult,
                            init,
                            eff,
                            sub_k,
                            0,
                        ]
                    )
                except Exception:
                    continue
    except (sqlite3.Error, ValueError, TypeError, OSError):
        pass
    finally:
        con.close()

    return {"sessions": sessions_meta, "calls": dict(calls_by_sid)}


def _apply_vscode_jsonl_mutation(root: Any, entry: dict[str, Any]) -> Any:
    """Replay one entry of VS Code's ObjectMutationLog (`kind`: 0=init, 1=set, 2=push, 3=delete)."""
    kind = entry.get("kind")
    if kind == 0:
        return entry.get("v")
    if root is None or not isinstance(entry.get("k"), list):
        return root
    path = entry["k"]
    if not path:
        return root
    cur = root
    try:
        for seg in path[:-1]:
            if isinstance(cur, list) and isinstance(seg, int):
                cur = cur[seg]
            elif isinstance(cur, dict):
                cur = cur[str(seg)]
            else:
                return root
        last = path[-1]
        if kind == 1:
            if isinstance(cur, list) and isinstance(last, int):
                if 0 <= last < len(cur):
                    cur[last] = entry.get("v")
                elif last == len(cur):
                    cur.append(entry.get("v"))
            elif isinstance(cur, dict):
                cur[str(last)] = entry.get("v")
        elif kind == 2:
            arr: Any = None
            if isinstance(cur, dict):
                arr = cur.get(str(last))
                if not isinstance(arr, list):
                    arr = []
                    cur[str(last)] = arr
            elif isinstance(cur, list) and isinstance(last, int) and 0 <= last < len(cur):
                arr = cur[last]
                if not isinstance(arr, list):
                    arr = []
                    cur[last] = arr
            if isinstance(arr, list):
                trunc_i = entry.get("i")
                if isinstance(trunc_i, int) and trunc_i >= 0:
                    del arr[trunc_i:]
                vals = entry.get("v")
                if isinstance(vals, list) and vals:
                    arr.extend(vals)
        elif kind == 3:
            if isinstance(cur, dict):
                cur.pop(str(last), None)
            elif isinstance(cur, list) and isinstance(last, int) and 0 <= last < len(cur):
                cur[last] = None
    except (IndexError, KeyError, TypeError):
        pass
    return root


def _first_int(*vals: Any) -> int | None:
    for v in vals:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    return None


def _extract_vscode_uri_str(val: Any) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        if val.get("fsPath"):
            return str(val["fsPath"])
        if val.get("path"):
            return str(val["path"])
        if val.get("external"):
            return str(val["external"])
    return ""


_VSC_DETAIL_MULT_RE = re.compile(r"•\s*([0-9]+(?:\.[0-9]+)?)\s*(?:x|credits?)\b", re.IGNORECASE)


def _pick_vscode_model(*cands: Any) -> str:
    for c in cands:
        if not isinstance(c, str):
            continue
        s = c.strip()
        if s and s.lower() not in ("auto", "copilot/auto", "github-copilot/auto"):
            return s
    return "copilot-chat"


def _parse_vscode_copilot_session(args: tuple[str, str]) -> dict[str, Any]:
    """
    Worker: parse a VS Code / Insiders / VSCodium `chatSessions/*.{jsonl,json}` file.
    Replays `ObjectMutationLog` deltas for `.jsonl` files and extracts GitHub Copilot turns,
    reconstructing full multi-round `toolCallRounds` context trajectories across both older
    (`github.copilot-chat` v0.33+) and newer integrated VS Code chat schemas.
    """
    path_str, ws_uri = args
    p = Path(path_str)
    session: Any = None
    try:
        if path_str.endswith(".jsonl"):
            with open(path_str, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        entry = json.loads(s)
                    except ValueError:
                        continue
                    if isinstance(entry, dict):
                        session = _apply_vscode_jsonl_mutation(session, entry)
        else:
            session = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        session = None

    if not isinstance(session, dict):
        return {"sid": p.stem, "cwd": ws_uri, "title": "", "creation_ms": 0, "calls": []}

    responder = str(session.get("responderUsername") or "").strip()
    resp_low = responder.lower()
    if resp_low and ("codex" in resp_low or "claude code" in resp_low):
        return {"sid": p.stem, "cwd": ws_uri, "title": "", "creation_ms": 0, "calls": []}

    sid = str(session.get("sessionId") or p.stem)
    creation_ms = to_epoch_ms(session.get("creationDate"))
    cwd = (
        ws_uri
        or _extract_vscode_uri_str(session.get("workingDirectory"))
        or _extract_vscode_uri_str(
            (session.get("repoData") or {}).get("rootUri") if isinstance(session.get("repoData"), dict) else ""
        )
    )
    title = str(session.get("customTitle") or session.get("computedTitle") or "").strip()
    prompt_fallback = ""

    inp_state = session.get("inputState") if isinstance(session.get("inputState"), dict) else {}
    sel_model_obj = inp_state.get("selectedModel") if isinstance(inp_state.get("selectedModel"), dict) else {}
    session_default_model = str(sel_model_obj.get("identifier") or "")

    calls: list[list[Any]] = []
    prev_req_prompt = 0
    requests = session.get("requests")
    if isinstance(requests, list):
        for req in requests:
            if not isinstance(req, dict):
                continue
            try:
                agent = req.get("agent") if isinstance(req.get("agent"), dict) else {}
                agent_id = str(agent.get("id") or "")
                if agent_id == "setup.agent":
                    continue
                ext_val = agent.get("extensionId")
                ext_id = (
                    str(ext_val.get("value") if isinstance(ext_val, dict) else (ext_val or "")).strip().lower()
                )
                if ext_id and not any(k in ext_id for k in ("copilot", "github", "vscode.chat")):
                    continue

                if not prompt_fallback:
                    msg = req.get("message")
                    if isinstance(msg, dict) and isinstance(msg.get("text"), str) and msg["text"].strip():
                        prompt_fallback = msg["text"].strip()[:120]
                    elif isinstance(msg, str) and msg.strip():
                        prompt_fallback = msg.strip()[:120]

                ts_ms = to_epoch_ms(req.get("responseTimestamp") or req.get("timestamp")) or creation_ms
                lmc = req.get("latestModelCall") if isinstance(req.get("latestModelCall"), dict) else {}
                auto_res = lmc.get("autoModeResolved") if isinstance(lmc.get("autoModeResolved"), dict) else {}
                mode_info = req.get("modeInfo") if isinstance(req.get("modeInfo"), dict) else {}
                m_cfg = req.get("modelConfiguration") if isinstance(req.get("modelConfiguration"), dict) else {}
                eff = str(
                    auto_res.get("reasoningBucket")
                    or mode_info.get("reasoningEffort")
                    or m_cfg.get("reasoningEffort")
                    or ""
                ).lower()

                res_obj = req.get("result") if isinstance(req.get("result"), dict) else {}
                meta_obj = res_obj.get("metadata") if isinstance(res_obj.get("metadata"), dict) else {}
                det_str = str(res_obj.get("details") or "").strip()
                det_model = det_str.split("•", 1)[0].strip() if "•" in det_str else ""

                mult_val = req.get("copilotCredits")
                if mult_val is None:
                    mult_val = lmc.get("cost")
                if isinstance(mult_val, (int, float)) and not isinstance(mult_val, bool):
                    mult = float(mult_val)
                else:
                    m_det = _VSC_DETAIL_MULT_RE.search(det_str)
                    mult = float(m_det.group(1)) if m_det else 0.0

                fallback_model = _pick_vscode_model(
                    auto_res.get("chosenModel"),
                    meta_obj.get("resolvedModel"),
                    meta_obj.get("modelId"),
                    meta_obj.get("model"),
                    res_obj.get("modelId"),
                    req.get("modelId"),
                    det_model,
                    session_default_model,
                )

                # 1) Prefer explicit per-model breakdown (`turnTokenTotals` / `modelTotals`) when available
                per_model_list = lmc.get("turnTokenTotals") or req.get("modelTotals")
                emitted_multi = False
                if isinstance(per_model_list, list) and per_model_list:
                    for idx_m, item in enumerate(per_model_list):
                        if not isinstance(item, dict):
                            continue
                        try:
                            m_name = _pick_vscode_model(item.get("model"), fallback_model)
                            cr = max(0, _first_int(item.get("cachedTokens"), item.get("cacheReadTokens")) or 0)
                            cw = max(0, _first_int(item.get("cacheCreationTokens"), item.get("cacheWriteTokens")) or 0)
                            r_in = max(0, _first_int(item.get("inputTokens"), item.get("promptTokens")) or 0)
                            inp = _copilot_uncached_in(r_in, cr, cw)
                            reas = max(0, _first_int(item.get("reasoningTokens")) or 0)
                            r_out = max(0, _first_int(item.get("outputTokens"), item.get("completionTokens")) or 0)
                            out_txt = max(0, r_out - reas)
                        except (ValueError, TypeError):
                            continue
                        if inp + cr + cw + out_txt + reas > 0:
                            calls.append(
                                [ts_ms, m_name, inp, cr, cw, out_txt, reas, mult if idx_m == 0 else 0.0, "user", eff, "", 0]
                            )
                            emitted_multi = True
                if emitted_multi:
                    continue

                u_obj = req.get("usage") if isinstance(req.get("usage"), dict) else {}
                res_u = (
                    res_obj.get("usage")
                    if isinstance(res_obj.get("usage"), dict)
                    else (meta_obj.get("usage") if isinstance(meta_obj.get("usage"), dict) else {})
                )
                ptd_candidates = (
                    req.get("promptTokenDetails"),
                    u_obj.get("promptTokenDetails"),
                    res_u.get("prompt_tokens_details"),
                    meta_obj.get("promptTokenDetails"),
                )
                ptd = next((d for d in ptd_candidates if isinstance(d, dict)), {})
                explicit_cr = max(
                    0,
                    _first_int(
                        ptd.get("cached_tokens"),
                        ptd.get("cachedTokens"),
                        u_obj.get("cachedTokens"),
                        u_obj.get("cacheReadTokens"),
                        res_u.get("cache_read_input_tokens"),
                        meta_obj.get("cachedTokens"),
                    )
                    or 0,
                )

                # 2) Multi-round agentic / chat turns (`result.metadata.toolCallRounds`)
                tcr_raw = meta_obj.get("toolCallRounds")
                tcr_dicts = [rd for rd in tcr_raw if isinstance(rd, dict)] if isinstance(tcr_raw, list) else []
                if tcr_dicts:
                    n_rounds = len(tcr_dicts)
                    timings = res_obj.get("timings") if isinstance(res_obj.get("timings"), dict) else {}
                    elapsed_ms = max(0, _first_int(timings.get("totalElapsed"), req.get("elapsedMs")) or 0)
                    tcr_results = (
                        meta_obj.get("toolCallResults") if isinstance(meta_obj.get("toolCallResults"), dict) else {}
                    )

                    rounds_meta: list[tuple[int, int, int, int]] = []  # (ts_ms, out_chars, reas_chars, tool_res_chars)
                    for idx_r, rd in enumerate(tcr_dicts):
                        rd_ts = to_epoch_ms(rd.get("timestamp"))
                        if not rd_ts:
                            rd_ts = ts_ms + ((elapsed_ms * idx_r) // max(1, n_rounds))
                        resp_s = str(rd.get("response") or "")
                        tcs_val = rd.get("toolCalls")
                        if isinstance(tcs_val, str):
                            try:
                                tcs_val = json.loads(tcs_val)
                            except ValueError:
                                tcs_val = []
                        tcs_list = tcs_val if isinstance(tcs_val, list) else []
                        tc_json = json.dumps(tcs_list, separators=(",", ":")) if tcs_list else ""
                        th_val = rd.get("thinking")
                        if isinstance(th_val, dict):
                            think_s = str(th_val.get("text") or "")
                        elif isinstance(th_val, str):
                            think_s = th_val
                        else:
                            think_s = ""
                        out_c = len(resp_s) + len(tc_json)
                        reas_c = len(think_s)
                        res_c = 0
                        for tc in tcs_list:
                            if isinstance(tc, dict):
                                tc_id = tc.get("id")
                                if tc_id and tc_id in tcr_results:
                                    res_c += len(json.dumps(tcr_results[tc_id], separators=(",", ":")))
                        rounds_meta.append((rd_ts, out_c, reas_c, res_c))

                    # Request-level `promptTokens`/`usage` is the turn total across all rounds;
                    # `result.metadata.promptTokens` is only the final round's context window snapshot.
                    turn_prompt_opt = _first_int(
                        req.get("promptTokens"),
                        u_obj.get("promptTokens"),
                        u_obj.get("prompt_tokens"),
                        u_obj.get("inputTokens"),
                        res_u.get("prompt_tokens"),
                        res_u.get("promptTokens"),
                        res_u.get("input_tokens"),
                    )
                    meta_final_prompt_opt = _first_int(
                        meta_obj.get("promptTokens"),
                        meta_obj.get("inputTokens"),
                    )
                    step_adds = [
                        max(1, (rounds_meta[k][1] + rounds_meta[k][2] + rounds_meta[k][3]) // 4)
                        for k in range(n_rounds)
                    ]
                    total_comp_opt = _first_int(
                        req.get("completionTokens"),
                        u_obj.get("completionTokens"),
                        u_obj.get("completion_tokens"),
                        u_obj.get("outputTokens"),
                        res_u.get("completion_tokens"),
                        res_u.get("completionTokens"),
                        res_u.get("output_tokens"),
                    )
                    last_out_opt = _first_int(meta_obj.get("outputTokens"), meta_obj.get("completionTokens"))

                    round_outs: list[tuple[int, int]] = []
                    out_is_est = 0
                    if total_comp_opt is not None and total_comp_opt > 0:
                        w_out = [max(1, rounds_meta[k][1] + rounds_meta[k][2]) for k in range(n_rounds)]
                        alloc_tot = _distribute_int(total_comp_opt, w_out)
                        for k in range(n_rounds):
                            tk = alloc_tot[k]
                            oc, rc = rounds_meta[k][1], rounds_meta[k][2]
                            rk = (tk * rc) // max(1, oc + rc) if rc > 0 else 0
                            round_outs.append((max(0, tk - rk), rk))
                    else:
                        out_is_est = 1
                        for k in range(n_rounds):
                            round_outs.append((rounds_meta[k][1] // 4, rounds_meta[k][2] // 4))
                        if last_out_opt is not None and last_out_opt > 0:
                            rc_last = round_outs[n_rounds - 1][1]
                            out_last = max(0, last_out_opt - rc_last) if last_out_opt >= rc_last else last_out_opt
                            round_outs[n_rounds - 1] = (out_last, rc_last)

                    if turn_prompt_opt is not None and turn_prompt_opt > 0:
                        ctx_w = [n_rounds + k for k in range(n_rounds)]
                        alloc_prompt = _distribute_int(turn_prompt_opt, ctx_w)
                        alloc_cr_turn = (
                            _distribute_int(min(turn_prompt_opt, explicit_cr), ctx_w)
                            if explicit_cr > 0 and explicit_cr <= turn_prompt_opt
                            else None
                        )
                        for k in range(n_rounds):
                            rd_ts = rounds_meta[k][0]
                            pk = alloc_prompt[k]
                            if alloc_cr_turn is not None:
                                cr_k = alloc_cr_turn[k]
                                inp_k = max(0, pk - cr_k)
                            elif explicit_cr > turn_prompt_opt:
                                # Anthropic-style exclusive input_tokens + separate cache_read
                                cr_k = _distribute_int(explicit_cr, ctx_w)[k]
                                inp_k = pk
                            else:
                                cr_k = (
                                    min(prev_req_prompt, max(0, pk - 1))
                                    if k == 0
                                    else min(alloc_prompt[k - 1], max(0, pk - 1))
                                )
                                inp_k = max(0, pk - cr_k)
                            out_k, reas_k = round_outs[k]
                            if inp_k + cr_k + out_k + reas_k > 0:
                                r_eff = eff or ("thinking" if reas_k > 0 else "")
                                calls.append(
                                    [
                                        rd_ts,
                                        fallback_model,
                                        inp_k,
                                        cr_k,
                                        0,
                                        out_k,
                                        reas_k,
                                        mult if k == 0 else 0.0,
                                        "user" if k == 0 else "agent",
                                        r_eff,
                                        "",
                                        out_is_est,
                                    ]
                                )
                        prev_req_prompt = alloc_prompt[-1] + step_adds[-1]
                    else:
                        prompt_at = [0] * n_rounds
                        if meta_final_prompt_opt is not None and meta_final_prompt_opt > 0:
                            prompt_at[n_rounds - 1] = meta_final_prompt_opt
                            for k in range(n_rounds - 2, -1, -1):
                                prompt_at[k] = max(256, prompt_at[k + 1] - step_adds[k])
                        else:
                            rum = meta_obj.get("renderedUserMessage")
                            rgc = meta_obj.get("renderedGlobalContext")
                            if rum or rgc:
                                init_c = (len(json.dumps(rum, separators=(",", ":"))) if rum else 0) + (
                                    len(json.dumps(rgc, separators=(",", ":"))) if rgc else 0
                                )
                            else:
                                msg_o = req.get("message")
                                vd_o = req.get("variableData")
                                init_c = (len(json.dumps(msg_o, separators=(",", ":"))) if msg_o else 0) + (
                                    len(json.dumps(vd_o, separators=(",", ":"))) if vd_o else 0
                                )
                            prompt_at[0] = prev_req_prompt + max(64, init_c // 4)
                            for k in range(1, n_rounds):
                                prompt_at[k] = prompt_at[k - 1] + step_adds[k - 1]

                        for k in range(n_rounds):
                            rd_ts = rounds_meta[k][0]
                            if k == 0:
                                cr_k = min(prev_req_prompt, max(0, prompt_at[0] - 1))
                                if n_rounds == 1 and explicit_cr > cr_k:
                                    cr_k = min(prompt_at[0], explicit_cr)
                            else:
                                cr_k = min(prompt_at[k - 1], prompt_at[k])
                            inp_k = max(0, prompt_at[k] - cr_k)
                            out_k, reas_k = round_outs[k]
                            if inp_k + cr_k + out_k + reas_k > 0:
                                r_eff = eff or ("thinking" if reas_k > 0 else "")
                                calls.append(
                                    [
                                        rd_ts,
                                        fallback_model,
                                        inp_k,
                                        cr_k,
                                        0,
                                        out_k,
                                        reas_k,
                                        mult if k == 0 else 0.0,
                                        "user" if k == 0 else "agent",
                                        r_eff,
                                        "",
                                        1,
                                    ]
                                )
                        prev_req_prompt = prompt_at[n_rounds - 1] + step_adds[n_rounds - 1]
                    continue

                # 3) Standard single-turn usage (`req.promptTokens`/`completionTokens`, `usage`, `result.metadata`)
                raw_in_opt = _first_int(
                    req.get("promptTokens"),
                    u_obj.get("promptTokens"),
                    u_obj.get("prompt_tokens"),
                    u_obj.get("inputTokens"),
                    res_u.get("prompt_tokens"),
                    res_u.get("promptTokens"),
                    res_u.get("input_tokens"),
                    meta_obj.get("promptTokens"),
                    meta_obj.get("inputTokens"),
                )
                raw_out_opt = _first_int(
                    req.get("completionTokens"),
                    u_obj.get("completionTokens"),
                    u_obj.get("completion_tokens"),
                    u_obj.get("outputTokens"),
                    res_u.get("completion_tokens"),
                    res_u.get("completionTokens"),
                    res_u.get("output_tokens"),
                    meta_obj.get("outputTokens"),
                    meta_obj.get("completionTokens"),
                )
                if raw_in_opt is None and raw_out_opt is None:
                    resp_arr = req.get("response")
                    resp_c = len(json.dumps(resp_arr, separators=(",", ":"))) if resp_arr else 0
                    msg_c = len(json.dumps(req.get("message") or "", separators=(",", ":")))
                    if resp_c > 32 and not req.get("isCanceled"):
                        inp_fb = max(1, msg_c // 4)
                        cr_fb = prev_req_prompt
                        out_fb = max(1, resp_c // 4)
                        prev_req_prompt += inp_fb + out_fb
                        calls.append([ts_ms, fallback_model, inp_fb, cr_fb, 0, out_fb, 0, mult, "user", eff, "", 1])
                    continue

                ctd = (
                    res_u.get("completion_tokens_details")
                    if isinstance(res_u.get("completion_tokens_details"), dict)
                    else (
                        u_obj.get("completion_tokens_details")
                        if isinstance(u_obj.get("completion_tokens_details"), dict)
                        else {}
                    )
                )
                cw = max(
                    0,
                    _first_int(
                        ptd.get("cache_creation_input_tokens"),
                        ptd.get("cache_creation_tokens"),
                        ptd.get("cacheCreationTokens"),
                        u_obj.get("cacheCreationTokens"),
                        u_obj.get("cacheWriteTokens"),
                        res_u.get("cache_creation_input_tokens"),
                    )
                    or 0,
                )
                raw_in = max(0, raw_in_opt or 0)
                cr = explicit_cr if explicit_cr > 0 else min(prev_req_prompt, max(0, raw_in - cw - 1))
                inp = _copilot_uncached_in(raw_in, cr, cw)
                reas = max(
                    0,
                    _first_int(
                        ctd.get("reasoning_tokens"),
                        ctd.get("reasoningTokens"),
                        res_u.get("reasoning_tokens"),
                        u_obj.get("reasoningTokens"),
                    )
                    or 0,
                )
                raw_out = max(0, raw_out_opt or 0)
                out_txt = max(0, raw_out - reas)
                if inp + cr + cw + out_txt + reas > 0:
                    prev_req_prompt = inp + cr + cw + raw_out
                    calls.append([ts_ms, fallback_model, inp, cr, cw, out_txt, reas, mult, "user", eff, "", 0])
            except Exception:
                continue

    return {
        "sid": sid,
        "cwd": cwd,
        "title": title or prompt_fallback,
        "creation_ms": creation_ms,
        "calls": calls,
    }


_JB_FILE_PATH_RE = re.compile(r'filePath="(/[^"]+)"')
_JB_UNIX_PATH_RE = re.compile(r"(/(?:home|Users)/[^\s\"'<>`;&|)]+)")


def _clean_jb_workspace_path(cand: str) -> str:
    """Clean a source path extracted from JetBrains Copilot prompts/tools into a repo/workspace root."""
    p = cand.strip().rstrip("\"'<>.,;:)`")
    if not p or not p.startswith("/"):
        return ""
    parts = p.split("/")
    # Collapse `/home/<user>/<src_dir>/<repo>/...` directly to `/home/<user>/<src_dir>/<repo>`
    if len(parts) >= 5 and parts[1] in ("home", "Users") and parts[3] in ("src", "code", "projects", "repos"):
        return "/".join(parts[:5])
    base = os.path.basename(p)
    if "." in base and not base.startswith("."):
        return os.path.dirname(p)
    return p


def _jb_dir_sig(conv_dir: Path) -> tuple[int, int] | None:
    """Combined `(max_mtime_ns, total_bytes)` signature across `partition-*.jsonl` in a JB conversation dir."""
    max_mtime = 0
    tot_size = 0
    found = False
    try:
        for entry in conv_dir.iterdir():
            if entry.is_file() and entry.name.startswith("partition-") and entry.name.endswith(".jsonl"):
                st = entry.stat()
                max_mtime = max(max_mtime, st.st_mtime_ns)
                tot_size += st.st_size
                found = True
    except OSError:
        return None
    return (max_mtime, tot_size) if found else None


def _parse_jetbrains_copilot_conv(conv_dir_str: str) -> dict[str, Any]:
    """
    Worker: parse JetBrains GitHub Copilot `<copilot_home>/jb/<cid>/partition-*.jsonl`.
    JetBrains Copilot logs verbatim `renderedMessage`, `assistant.message` (`text` + `thinking`),
    and `tool.execution_start` / `tool.execution_complete` without raw token integers; reconstructs
    per-call cumulative prompt cache (`chars // 4`), uncached delta, output, and reasoning tokens.
    """
    cdir = Path(conv_dir_str)
    cid = cdir.name
    has_partition_created = False
    created_ms = 0
    start_ms = 0
    end_ms = 0
    cwd = ""
    title = ""
    prompt_fallback = ""
    saw_anthropic = False
    calls: list[list[Any]] = []
    subagent_windows: list[list[Any]] = []
    active_subagents: dict[str, tuple[int, str]] = {}

    def _part_sort_key(p: Path) -> tuple[int, str]:
        stem = p.stem
        if "-" in stem:
            try:
                return (int(stem.split("-", 1)[1]), p.name)
            except ValueError:
                pass
        return (999999, p.name)

    try:
        part_files = sorted(
            [
                e
                for e in cdir.iterdir()
                if e.is_file() and e.name.startswith("partition-") and e.name.endswith(".jsonl")
            ],
            key=_part_sort_key,
        )
    except OSError:
        part_files = []

    history_chars = 0
    pending_fresh_chars = 0

    for pf in part_files:
        try:
            with open(pf, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        ev = json.loads(s)
                    except ValueError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    try:
                        etype = ev.get("type") or ""
                        ts_ms = to_epoch_ms(ev.get("timestamp"))
                        if ts_ms:
                            start_ms = min(start_ms, ts_ms) if start_ms else ts_ms
                            end_ms = max(end_ms, ts_ms)
                        data = ev.get("data")
                        if not isinstance(data, dict):
                            data = {}

                        if etype == "partition.created":
                            has_partition_created = True
                            if data.get("conversationId"):
                                cid = str(data["conversationId"])
                            c_at = to_epoch_ms(data.get("createdAt")) or ts_ms
                            if c_at and not created_ms:
                                created_ms = c_at
                        elif etype == "user.message":
                            c_val = data.get("content") or ""
                            if isinstance(c_val, str) and c_val.strip() and not title:
                                title = c_val.strip()[:120]
                        elif etype == "user.message_rendered":
                            rm_str = str(data.get("renderedMessage") or "")
                            pending_fresh_chars += len(rm_str)
                            if not prompt_fallback and "<userRequest>" in rm_str and "</userRequest>" in rm_str:
                                ur = rm_str.split("<userRequest>", 1)[1].split("</userRequest>", 1)[0].strip()
                                if ur:
                                    prompt_fallback = ur[:120]
                            if not cwd and rm_str:
                                m_fp = _JB_FILE_PATH_RE.search(rm_str) or _JB_UNIX_PATH_RE.search(rm_str)
                                if m_fp:
                                    cwd = _clean_jb_workspace_path(m_fp.group(1))
                        elif etype == "assistant.message":
                            text_s = str(data.get("text") or data.get("content") or "")
                            thinking_obj = data.get("thinking")
                            think_s = ""
                            if isinstance(thinking_obj, dict):
                                saw_anthropic = True
                                think_s = str(thinking_obj.get("text") or "")
                            elif isinstance(thinking_obj, str):
                                think_s = thinking_obj
                            explicit_m = str(data.get("model") or "").strip()
                            raw_m = explicit_m or ("claude-(unknown)" if saw_anthropic else "copilot-jetbrains")
                            cr = history_chars // 4
                            inp = max(1, pending_fresh_chars // 4)
                            out_txt = len(text_s) // 4
                            reas = len(think_s) // 4
                            iter_n = int(data.get("iterationNumber") or 0)
                            eff = "thinking" if think_s else ""
                            eff_ts = ts_ms or end_ms or created_ms
                            calls.append(
                                [
                                    eff_ts,
                                    raw_m,
                                    inp,
                                    cr,
                                    0,
                                    out_txt,
                                    reas,
                                    0.0,
                                    "user" if iter_n == 1 else "agent",
                                    eff,
                                    "",
                                    1,
                                ]
                            )
                            history_chars += pending_fresh_chars + len(text_s) + len(think_s)
                            pending_fresh_chars = 0
                        elif etype == "tool.execution_start":
                            tc_id = str(data.get("toolCallId") or "")
                            if tc_id.startswith("toolu_"):
                                saw_anthropic = True
                            t_name = str(data.get("toolName") or "")
                            args = data.get("arguments")
                            args_str = json.dumps(args, separators=(",", ":")) if args is not None else ""
                            if args_str:
                                if calls:
                                    calls[-1][5] += len(args_str) // 4
                                history_chars += len(args_str)
                            if not cwd and isinstance(args, dict):
                                cand_p = str(args.get("path") or args.get("filePath") or "")
                                if cand_p.startswith("/"):
                                    cwd = _clean_jb_workspace_path(cand_p)
                                elif args.get("command"):
                                    m_cmd = _JB_UNIX_PATH_RE.search(str(args["command"]))
                                    if m_cmd:
                                        cwd = _clean_jb_workspace_path(m_cmd.group(1))
                            if t_name == "run_subagent" and tc_id:
                                ag_lbl = str(args.get("agentName") or "subagent") if isinstance(args, dict) else "subagent"
                                active_subagents[tc_id] = (ts_ms or end_ms, ag_lbl)
                        elif etype == "tool.execution_complete":
                            tc_id = str(data.get("toolCallId") or "")
                            res_obj = data.get("result")
                            if res_obj is not None:
                                pending_fresh_chars += len(json.dumps(res_obj, separators=(",", ":")))
                            if tc_id in active_subagents:
                                st_ms, ag_lbl = active_subagents.pop(tc_id)
                                subagent_windows.append([st_ms, ts_ms or end_ms or st_ms, ag_lbl])
                    except Exception:
                        continue
        except OSError:
            pass

    for _tc_id, (st_ms, ag_lbl) in active_subagents.items():
        subagent_windows.append([st_ms, end_ms or st_ms, ag_lbl])

    if saw_anthropic:
        for c in calls:
            if c[1] == "copilot-jetbrains":
                c[1] = "claude-(unknown)"

    if not has_partition_created:
        for c in calls:
            c[7] = 0.0

    return {
        "sid": cid,
        "has_partition_created": has_partition_created,
        "created_ms": created_ms or start_ms,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "cwd": cwd,
        "title": title or prompt_fallback,
        "calls": calls,
        "subagent_windows": subagent_windows,
    }


_COPILOT_LOG_SKU_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+\S+\s+\[info\]\s+copilot token\b.*?\bsku:\s*([A-Za-z0-9_]+)"
)


def _parse_copilot_chat_log(path_str: str) -> dict[str, Any]:
    """Worker: extract `(date_iso, sku)` billing-tier samples from `GitHub Copilot Chat.log`."""
    seen: set[tuple[str, str]] = set()
    samples: list[list[str]] = []
    try:
        with open(path_str, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "sku:" not in line:
                    continue
                m = _COPILOT_LOG_SKU_RE.match(line)
                if m:
                    pair = (m.group(1), m.group(2))
                    if pair not in seen:
                        seen.add(pair)
                        samples.append([pair[0], pair[1]])
    except OSError:
        pass
    return {"samples": samples}


def extract_copilot(cache_con: sqlite3.Connection, pool: ProcessPoolExecutor | None) -> dict[str, Any]:
    """
    Extract GitHub Copilot telemetry across:
      1. GitHub Copilot CLI / VS Code Agent (`$COPILOT_HOME/state/session-store.db` + `session-state/*/events.jsonl`)
      2. JetBrains GitHub Copilot (`$COPILOT_HOME/jb/*/partition-*.jsonl`)
      3. VS Code / Code - Insiders / VSCodium Copilot Chat (`chatSessions/*.{jsonl,json}` + `logs/**/GitHub Copilot Chat.log`)
    """
    t0 = time.time()
    env_cop = os.environ.get("COPILOT_HOME")
    copilot_home = Path(env_cop).expanduser() if env_cop else (HOME / ".copilot")

    copilot_roots: list[Path] = []
    seen_cop_roots: set[Path] = set()
    for cand_root in [copilot_home, *[b / ".copilot" for b in get_backup_roots()]]:
        if not cand_root.is_dir():
            continue
        try:
            rp = cand_root.resolve()
        except OSError:
            rp = cand_root
        if rp not in seen_cop_roots:
            seen_cop_roots.add(rp)
            copilot_roots.append(cand_root)

    # 1. Discover Copilot CLI SQLite session stores, session-state/*/events.jsonl, and JetBrains jb/*
    cli_db_paths: list[str] = []
    cli_event_files: list[str] = []
    jb_conv_dirs: list[Path] = []
    seen_jb_ids: set[str] = set()

    for croot in copilot_roots:
        for cand_db in (croot / "state" / "session-store.db", croot / "session-store.db"):
            if cand_db.is_file():
                cli_db_paths.append(str(cand_db))
                break
        sess_state_root = croot / "session-state"
        if sess_state_root.is_dir():
            cli_event_files.extend(sorted(glob.glob(str(sess_state_root / "*" / "events.jsonl"))))
        jb_root = croot / "jb"
        if jb_root.is_dir():
            try:
                for entry in sorted(jb_root.iterdir(), key=lambda x: x.name):
                    if entry.is_dir() and entry.name not in seen_jb_ids:
                        seen_jb_ids.add(entry.name)
                        jb_conv_dirs.append(entry)
            except OSError:
                pass

    # 2. Discover VS Code / Insiders / VSCodium chatSessions/*.{jsonl,json} (live + backup `.config*` roots)
    vsc_tasks: list[tuple[str, str]] = []  # (file_path, workspace_uri)
    chat_log_files: list[str] = []
    ed_names = ("Code", "Code - Insiders", "VSCodium")
    backup_roots = get_backup_roots()
    backup_cfg_dirs: list[Path] = []
    for b in backup_roots:
        try:
            backup_cfg_dirs.extend(sorted(d for d in b.glob(".config*") if d.is_dir()))
        except OSError:
            pass
    cand_editor_roots = [
        *[HOME / ".config" / ed / "User" for ed in ed_names],
        *[HOME / "Library" / "Application Support" / ed / "User" for ed in ed_names],
        *[cfg_d / ed / "User" for cfg_d in backup_cfg_dirs for ed in ed_names],
        *[b / ed / "User" for b in backup_roots for ed in ed_names],
        *[b / "User" for b in backup_roots],
    ]
    editor_roots: list[Path] = []
    seen_ed_roots: set[Path] = set()
    for er in cand_editor_roots:
        if not er.is_dir():
            continue
        try:
            erp = er.resolve()
        except OSError:
            erp = er
        if erp not in seen_ed_roots:
            seen_ed_roots.add(erp)
            editor_roots.append(er)

    def _collect_chat_dir(chat_dir: Path, ws_uri: str) -> None:
        if not chat_dir.is_dir():
            return
        try:
            entries = sorted(chat_dir.iterdir(), key=lambda x: x.name)
        except OSError:
            return
        jsonl_stems = {e.stem for e in entries if e.suffix == ".jsonl" and e.is_file()}
        for e in entries:
            if not e.is_file():
                continue
            if e.suffix == ".jsonl":
                vsc_tasks.append((str(e), ws_uri))
            elif e.suffix == ".json" and e.stem not in jsonl_stems:
                vsc_tasks.append((str(e), ws_uri))

    seen_log_dirs: set[Path] = set()
    for user_root in editor_roots:
        if not user_root.is_dir():
            continue
        ws_storage = user_root / "workspaceStorage"
        if ws_storage.is_dir():
            try:
                ws_dirs = sorted(ws_storage.iterdir(), key=lambda x: x.name)
            except OSError:
                ws_dirs = []
            for wd in ws_dirs:
                chat_dir = wd / "chatSessions"
                if not chat_dir.is_dir():
                    continue
                ws_uri = ""
                ws_json = wd / "workspace.json"
                if ws_json.is_file():
                    try:
                        wobj = json.loads(ws_json.read_text(encoding="utf-8", errors="replace"))
                        if isinstance(wobj, dict):
                            ws_uri = str(wobj.get("folder") or wobj.get("workspace") or "")
                    except (OSError, ValueError):
                        pass
                _collect_chat_dir(chat_dir, ws_uri)
        _collect_chat_dir(user_root / "globalStorage" / "emptyWindowChatSessions", "")
        logs_dir = user_root.parent / "logs"
        if logs_dir.is_dir() and logs_dir not in seen_log_dirs:
            seen_log_dirs.add(logs_dir)
            try:
                chat_log_files.extend(
                    str(p) for p in sorted(logs_dir.rglob("GitHub Copilot Chat.log")) if p.is_file()
                )
            except OSError:
                pass

    cached_rows = {
        row[0]: (row[1], row[2], row[3])
        for row in cache_con.execute(
            "SELECT path, mtime_ns, size, payload_json FROM file_cache WHERE harness = 'copilot'"
        )
    }

    active_paths: set[str] = set()
    cache_hits = 0
    inserts: list[tuple[str, str, int, int, str]] = []

    # Parse / load cached CLI SQLite DBs (live first, then backups)
    db_sessions_meta: dict[str, dict[str, str]] = {}
    db_calls_by_sid: dict[str, list[list[Any]]] = {}
    files_parsed = 0
    for db_p in cli_db_paths:
        sig = _file_sig(db_p)
        if sig is None:
            continue
        active_paths.add(db_p)
        mtime_ns, size = sig
        c_hit = cached_rows.get(db_p)
        if c_hit and c_hit[0] == mtime_ns and c_hit[1] == size:
            db_pl = json.loads(c_hit[2])
            cache_hits += 1
        else:
            db_pl = _parse_copilot_store_db(db_p)
            files_parsed += 1
            inserts.append(("copilot", db_p, mtime_ns, size, json.dumps(db_pl, separators=(",", ":"))))
        for k, v in (db_pl.get("sessions") or {}).items():
            db_sessions_meta.setdefault(k, v)
        for k, v in (db_pl.get("calls") or {}).items():
            if k not in db_calls_by_sid or len(v) > len(db_calls_by_sid[k]):
                db_calls_by_sid[k] = v

    # Parse / load cached CLI events.jsonl (keeping richest payload per sid, tie-breaking to live)
    cli_payloads: dict[str, dict[str, Any]] = {}
    to_parse_cli: list[tuple[str, int, int]] = []

    def _merge_cli_pl(sid_key: str, pl_obj: dict[str, Any]) -> None:
        prev = cli_payloads.get(sid_key)
        if prev is None or len(pl_obj.get("calls") or []) > len(prev.get("calls") or []):
            cli_payloads[sid_key] = pl_obj

    for ep in cli_event_files:
        sig = _file_sig(ep)
        if sig is None:
            continue
        active_paths.add(ep)
        mtime_ns, size = sig
        c_hit = cached_rows.get(ep)
        if c_hit and c_hit[0] == mtime_ns and c_hit[1] == size:
            pl = json.loads(c_hit[2])
            _merge_cli_pl(pl.get("sid") or Path(ep).parent.name, pl)
            cache_hits += 1
        else:
            to_parse_cli.append((ep, mtime_ns, size))

    if to_parse_cli:
        c_paths = [x[0] for x in to_parse_cli]
        fresh_cli = (
            list(pool.map(_parse_copilot_cli_events, c_paths, chunksize=20))
            if pool
            else [_parse_copilot_cli_events(x) for x in c_paths]
        )
        files_parsed += len(to_parse_cli)
        for (ep, mtime_ns, size), pl in zip(to_parse_cli, fresh_cli):
            _merge_cli_pl(pl.get("sid") or Path(ep).parent.name, pl)
            inserts.append(("copilot", ep, mtime_ns, size, json.dumps(pl, separators=(",", ":"))))

    # Parse / load cached JetBrains Copilot `jb/<cid>/partition-*.jsonl`
    jb_payloads: list[dict[str, Any]] = []
    to_parse_jb: list[tuple[str, int, int]] = []
    for jdir in jb_conv_dirs:
        jsig = _jb_dir_sig(jdir)
        if jsig is None:
            continue
        jkey = str(jdir)
        active_paths.add(jkey)
        mtime_ns, size = jsig
        c_hit = cached_rows.get(jkey)
        if c_hit and c_hit[0] == mtime_ns and c_hit[1] == size:
            jb_payloads.append(json.loads(c_hit[2]))
            cache_hits += 1
        else:
            to_parse_jb.append((jkey, mtime_ns, size))

    if to_parse_jb:
        j_paths = [x[0] for x in to_parse_jb]
        fresh_jb = (
            list(pool.map(_parse_jetbrains_copilot_conv, j_paths, chunksize=20))
            if pool
            else [_parse_jetbrains_copilot_conv(x) for x in j_paths]
        )
        files_parsed += len(to_parse_jb)
        for (jkey, mtime_ns, size), pl in zip(to_parse_jb, fresh_jb):
            jb_payloads.append(pl)
            inserts.append(("copilot", jkey, mtime_ns, size, json.dumps(pl, separators=(",", ":"))))

    # Parse / load cached VS Code chatSessions
    vsc_payloads: list[dict[str, Any]] = []
    to_parse_vsc: list[tuple[str, str, int, int]] = []
    for vp, ws_uri in vsc_tasks:
        sig = _file_sig(vp)
        if sig is None:
            continue
        active_paths.add(vp)
        mtime_ns, size = sig
        c_hit = cached_rows.get(vp)
        if c_hit and c_hit[0] == mtime_ns and c_hit[1] == size:
            pl = json.loads(c_hit[2])
            if ws_uri and not pl.get("cwd"):
                pl["cwd"] = ws_uri
            vsc_payloads.append(pl)
            cache_hits += 1
        else:
            to_parse_vsc.append((vp, ws_uri, mtime_ns, size))

    if to_parse_vsc:
        v_args = [(x[0], x[1]) for x in to_parse_vsc]
        fresh_vsc = (
            list(pool.map(_parse_vscode_copilot_session, v_args, chunksize=25))
            if pool
            else [_parse_vscode_copilot_session(x) for x in v_args]
        )
        files_parsed += len(to_parse_vsc)
        for (vp, _ws_uri, mtime_ns, size), pl in zip(to_parse_vsc, fresh_vsc):
            vsc_payloads.append(pl)
            inserts.append(("copilot", vp, mtime_ns, size, json.dumps(pl, separators=(",", ":"))))

    # Parse / load cached VS Code `GitHub Copilot Chat.log` SKU billing telemetry
    sku_dates: dict[str, list[str]] = defaultdict(list)
    to_parse_logs: list[tuple[str, int, int]] = []
    for lp in chat_log_files:
        sig = _file_sig(lp)
        if sig is None:
            continue
        active_paths.add(lp)
        mtime_ns, size = sig
        c_hit = cached_rows.get(lp)
        if c_hit and c_hit[0] == mtime_ns and c_hit[1] == size:
            lpl = json.loads(c_hit[2])
            for d_iso, sku_s in lpl.get("samples") or []:
                sku_dates[str(sku_s)].append(str(d_iso))
            cache_hits += 1
        else:
            to_parse_logs.append((lp, mtime_ns, size))

    if to_parse_logs:
        l_paths = [x[0] for x in to_parse_logs]
        fresh_logs = (
            list(pool.map(_parse_copilot_chat_log, l_paths, chunksize=20))
            if pool
            else [_parse_copilot_chat_log(x) for x in l_paths]
        )
        files_parsed += len(to_parse_logs)
        for (lp, mtime_ns, size), lpl in zip(to_parse_logs, fresh_logs):
            for d_iso, sku_s in lpl.get("samples") or []:
                sku_dates[str(sku_s)].append(str(d_iso))
            inserts.append(("copilot", lp, mtime_ns, size, json.dumps(lpl, separators=(",", ":"))))

    sku_segments = [
        {"sku": sku, "first_date": min(ds), "last_date": max(ds)}
        for sku, ds in sorted(sku_dates.items(), key=lambda kv: (min(kv[1]), kv[0]))
        if ds
    ]

    if inserts:
        cache_con.executemany(
            "INSERT OR REPLACE INTO file_cache(harness, path, mtime_ns, size, payload_json) VALUES (?, ?, ?, ?, ?)",
            inserts,
        )
        cache_con.commit()

    _prune_file_cache(cache_con, "copilot", active_paths, set(cached_rows.keys()))

    cube_acc: dict[tuple[str, str, str, str, str, str, int], list[float]] = defaultdict(
        lambda: [0, 0, 0, 0, 0, 0, 0.0]
    )
    sessions_out: list[dict[str, Any]] = []
    project_raw_paths: dict[str, set[str]] = defaultdict(set)
    timelines = new_timelines()
    total_calls = 0
    estimated_calls = 0
    estimated_tokens = 0
    premium_requests = 0.0
    cli_sessions_count = 0
    jetbrains_sessions_count = 0
    vscode_sessions_count = 0
    min_date, max_date = "9999-99-99", "0000-00-00"

    def _emit_session_group(
        sid: str,
        parent_sid: str,
        raw_cwd: str,
        base_title: str,
        fallback_date: str,
        raw_calls: list[list[Any]],
        client_label: str = "Copilot CLI",
        force_subagent: bool = False,
    ) -> bool:
        nonlocal total_calls, estimated_calls, estimated_tokens, premium_requests, min_date, max_date
        if not raw_calls:
            return False
        proj, clean_path = normalize_project(raw_cwd)
        if clean_path:
            project_raw_paths[proj].add(clean_path)

        # Group calls into main session + any subagent lanes (`sub_k`)
        by_agent: dict[str, list[list[Any]]] = defaultdict(list)
        for c in raw_calls:
            sub_k = str(c[10] or "")
            if sub_k and not parent_sid and not force_subagent:
                ag_sid = f"{sid}:sub:{sub_k}"
            else:
                ag_sid = sid
            by_agent[ag_sid].append(c)

        emitted_any = False
        for ag_sid, ag_calls in by_agent.items():
            is_sub_lane = ag_sid != sid or bool(parent_sid) or force_subagent
            ag_parent = sid if ag_sid != sid else parent_sid
            actor = "subagent" if is_sub_lane else "main"

            s_calls = s_in = s_cr = s_cw = s_out = s_reas = 0
            mp_toks: dict[tuple[str, str], int] = defaultdict(int)
            mp_max_date: dict[tuple[str, str], str] = {}

            for rec in ag_calls:
                ts_val, raw_m, inp, cr, cw, out_txt, reas, mult, _init, eff, _sub_k = rec[:11]
                is_est = int(rec[11]) if len(rec) > 11 else 0
                ep_ms = to_epoch_ms(ts_val)
                d_key = epoch_to_local_hour_key(ep_ms) if ep_ms > 0 else iso_to_local_hour_key(str(ts_val))
                eff_key = d_key if d_key != "unknown" else fallback_date
                eff_date = hour_key_date(eff_key)
                if eff_date != "unknown":
                    min_date = min(min_date, eff_date)
                    max_date = max(max_date, eff_date)

                model, provider = normalize_model_and_provider(raw_m, None, "GitHub Copilot")
                key = (eff_key, "GitHub Copilot", provider, model, proj, actor, 0)
                tl = timelines[ag_sid]
                tl["parent"] = ag_parent
                tl["calls"].append([ep_ms, model, inp, cr, cw, out_txt, reas, 0.0, eff])

                b = cube_acc[key]
                b[0] += 1
                b[1] += inp
                b[2] += cr
                b[3] += cw
                b[4] += out_txt
                b[5] += reas

                s_calls += 1
                s_in += inp
                s_cr += cr
                s_cw += cw
                s_out += out_txt
                s_reas += reas
                premium_requests += float(mult or 0.0)
                tok_sum = inp + cr + cw + out_txt + reas
                if is_est:
                    estimated_calls += 1
                    estimated_tokens += tok_sum
                mp = (model, provider)
                mp_toks[mp] += tok_sum
                prev_d = mp_max_date.get(mp)
                if prev_d is None or (eff_date != "unknown" and (prev_d == "unknown" or eff_date > prev_d)):
                    mp_max_date[mp] = eff_date
                total_calls += 1

            if s_in + s_cr + s_cw + s_out + s_reas > 0:
                dom_model, dom_provider = max(mp_toks.items(), key=lambda kv: kv[1])[0]
                dom_date = mp_max_date.get((dom_model, dom_provider), fallback_date)
                if ag_sid != sid:
                    lbl = f"Copilot subagent {ag_sid.rsplit(':', 1)[-1][:24]}"
                else:
                    lbl = (base_title or f"{client_label} {sid[:8]}").strip()
                sessions_out.append(
                    {
                        "id": ag_sid,
                        "harness": "GitHub Copilot",
                        "title": lbl.replace("\n", " ")[:120],
                        "project": proj,
                        "model": dom_model,
                        "provider": dom_provider,
                        "actor": actor,
                        "date": dom_date,
                        "calls": s_calls,
                        "input_uncached": s_in,
                        "cache_read": s_cr,
                        "cache_write": s_cw,
                        "output_text": s_out,
                        "reasoning": s_reas,
                        "cost_usd": 0.0,
                    }
                )
                emitted_any = True
        return emitted_any

    # Merge Copilot CLI sessions across session-store.db and session-state/*/events.jsonl
    all_cli_sids = sorted(set(db_sessions_meta.keys()) | set(db_calls_by_sid.keys()) | set(cli_payloads.keys()))
    emitted_sids: set[str] = set()
    for sid in all_cli_sids:
        db_m = db_sessions_meta.get(sid) or {}
        ev_pl = cli_payloads.get(sid) or {}
        parent_sid = str(ev_pl.get("parent_sid") or "")
        raw_cwd = str(ev_pl.get("cwd") or db_m.get("cwd") or "")
        if not raw_cwd and parent_sid:
            p_ev = cli_payloads.get(parent_sid) or {}
            p_db = db_sessions_meta.get(parent_sid) or {}
            raw_cwd = str(p_ev.get("cwd") or p_db.get("cwd") or "")
        title = str(
            ev_pl.get("title")
            or db_m.get("summary")
            or db_m.get("prompt")
            or ""
        )
        fallback_raw = (
            ev_pl.get("last_ts")
            or db_m.get("updated_at")
            or ev_pl.get("start_time")
            or db_m.get("created_at")
            or ""
        )
        fb_ms = to_epoch_ms(fallback_raw)
        fallback_iso = str(fallback_raw)
        fallback_date = epoch_to_local_date(fb_ms) if fb_ms > 0 else iso_to_local_date(fallback_iso)

        # Prefer per-turn SQLite `assistant_usage_events`; otherwise use segment-reconciled `events.jsonl` turns
        from_db = bool(db_calls_by_sid.get(sid))
        calls = list(db_calls_by_sid.get(sid) or ev_pl.get("calls") or [])

        # Reconcile SQLite `db_calls_by_sid` against `session.shutdown` totals (events.jsonl payloads are already segment-reconciled)
        shutdown_metrics = ev_pl.get("shutdown_metrics") or {}
        if shutdown_metrics and (from_db or not ev_pl.get("segments_reconciled")):
            recorded_by_model: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            for c in calls:
                rm = str(c[1])
                rec_m = recorded_by_model[rm]
                rec_m[0] += float(c[7] or 0.0)
                rec_m[1] += c[2]
                rec_m[2] += c[3]
                rec_m[3] += c[4]
                rec_m[4] += c[5]
                rec_m[5] += c[6]
            sh_ts = ev_pl.get("shutdown_ts") or fallback_raw
            def_eff = str(ev_pl.get("default_effort") or "")
            for rm, vals in shutdown_metrics.items():
                _r_cnt, r_cost, m_inp, m_cr, m_cw, m_out, m_reas = vals
                got = recorded_by_model.get(rm) or [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
                d_inp = max(0, int(m_inp - got[1]))
                d_cr = max(0, int(m_cr - got[2]))
                d_cw = max(0, int(m_cw - got[3]))
                d_out = max(0, int(m_out - got[4]))
                d_reas = max(0, int(m_reas - got[5]))
                d_mult = max(0.0, float(r_cost - got[0]))
                if d_inp + d_cr + d_cw + d_out + d_reas > 0:
                    calls.append([sh_ts, rm, d_inp, d_cr, d_cw, d_out, d_reas, d_mult, "user", def_eff, "", 0])

        if _emit_session_group(sid, parent_sid, raw_cwd, title, fallback_date, calls, client_label="Copilot CLI"):
            emitted_sids.add(sid)
            cli_sessions_count += 1

    # Emit JetBrains GitHub Copilot conversations (`jb/<cid>/partition-*.jsonl`), linking subagent windows
    jb_parents = [pl for pl in jb_payloads if pl.get("has_partition_created")]
    for pl in jb_payloads:
        sid = str(pl.get("sid") or "")
        if not sid or sid in emitted_sids:
            continue
        calls = pl.get("calls") or []
        if not calls:
            continue
        is_sub = not bool(pl.get("has_partition_created"))
        parent_sid = ""
        raw_cwd = str(pl.get("cwd") or "")
        base_title = str(pl.get("title") or "")
        if is_sub:
            sub_st = int(pl.get("start_ms") or 0)
            for p_cand in jb_parents:
                for win in p_cand.get("subagent_windows") or []:
                    w_st, w_end, ag_lbl = int(win[0] or 0), int(win[1] or 0), str(win[2] or "subagent")
                    if sub_st and w_st and (w_st - 5000 <= sub_st <= w_end + 5000):
                        parent_sid = str(p_cand.get("sid") or "")
                        if not raw_cwd:
                            raw_cwd = str(p_cand.get("cwd") or "")
                        if not base_title:
                            base_title = f"JetBrains Copilot {ag_lbl} ({sid[:8]})"
                        break
                if parent_sid:
                    break
            if not base_title:
                base_title = f"JetBrains Copilot subagent {sid[:8]}"

        fallback_date = epoch_to_local_date(pl.get("created_ms") or pl.get("start_ms") or 0)
        if _emit_session_group(
            sid,
            parent_sid,
            raw_cwd,
            base_title,
            fallback_date,
            calls,
            client_label="JetBrains Copilot",
            force_subagent=is_sub,
        ):
            emitted_sids.add(sid)
            jetbrains_sessions_count += 1

    # Deduplicate VS Code chatSessions across migrated workspaceStorage directories by `sid`,
    # selecting the richest call stream and adopting any resolved non-(unassigned) project path.
    vsc_by_sid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pl in vsc_payloads:
        sid_k = str(pl.get("sid") or "")
        if sid_k:
            vsc_by_sid[sid_k].append(pl)

    for sid, group in vsc_by_sid.items():
        if sid in emitted_sids:
            continue
        best_pl = max(
            group,
            key=lambda p: (
                len(p.get("calls") or []),
                sum(sum(c[2:7]) for c in (p.get("calls") or [])),
            ),
        )
        calls = best_pl.get("calls") or []
        if not calls:
            continue
        best_cwd = str(best_pl.get("cwd") or "")
        if normalize_project(best_cwd)[0] == "(unassigned)":
            for sib in group:
                sib_cwd = str(sib.get("cwd") or "")
                if sib_cwd and normalize_project(sib_cwd)[0] != "(unassigned)":
                    best_cwd = sib_cwd
                    break
        best_title = str(best_pl.get("title") or "")
        if not best_title:
            for sib in group:
                if sib.get("title"):
                    best_title = str(sib["title"])
                    break
        fallback_date = epoch_to_local_date(best_pl.get("creation_ms") or 0)
        if _emit_session_group(
            sid,
            "",
            best_cwd,
            best_title,
            fallback_date,
            calls,
            client_label="VS Code Copilot",
        ):
            emitted_sids.add(sid)
            vscode_sessions_count += 1

    elapsed_ms = round((time.time() - t0) * 1000, 1)
    return {
        "cube": cube_acc,
        "sessions": sessions_out,
        "timelines": timelines,
        "project_paths": project_raw_paths,
        "copilot_billing": {
            "premium_requests": round(premium_requests, 2),
            "cli_sessions": cli_sessions_count,
            "jetbrains_sessions": jetbrains_sessions_count,
            "vscode_sessions": vscode_sessions_count,
            "estimated_calls": estimated_calls,
            "estimated_tokens": estimated_tokens,
            "sku_segments": sku_segments,
            "overage_rate_per_request_usd": 0.04,
        },
        "health": {
            "harness": "GitHub Copilot",
            "source_path": "~/.copilot (session-store.db, session-state, jb) + VS Code chatSessions",
            "files_total": len(active_paths),
            "cache_hits": cache_hits,
            "files_parsed": files_parsed,
            "sessions_count": len(sessions_out),
            "api_calls": total_calls,
            "live_date_range": f"{min_date} .. {max_date}" if sessions_out else "n/a",
            "elapsed_ms": elapsed_ms,
        },
    }


# ---------------------------------------------------------------------------
# Reference API Rate Estimator Defaults ($ per 1M tokens)
# ---------------------------------------------------------------------------


def default_model_pricing(model: str, provider: str) -> dict[str, Any]:
    """
    Reference API-equivalent rates ($ per 1M tokens) for "what-if API spend" estimation.
    Calibrated against exact logged rates in opencode.db (e.g. claude-sonnet-5-5 = $2/$0.20/$2.50/$10)
    and standard provider tier pricing; editable interactively in the web UI.
    """
    ml = model.lower()
    if provider.startswith("Local"):
        return {"in": 0.0, "cr": 0.0, "cw": 0.0, "out": 0.0, "note": "Local self-hosted GPU ($0 API)"}
    if "haiku" in ml:
        return {"in": 0.80, "cr": 0.08, "cw": 1.00, "out": 4.00, "note": "Haiku tier"}
    if "sonnet" in ml:
        return {"in": 2.00, "cr": 0.20, "cw": 2.50, "out": 10.00, "note": "Calibrated from OpenCode billing"}
    if "opus" in ml:
        return {"in": 5.00, "cr": 0.50, "cw": 6.25, "out": 25.00, "note": "Opus tier (calibrated)"}
    if "fable" in ml:
        return {"in": 5.00, "cr": 0.50, "cw": 6.25, "out": 25.00, "note": "Fable frontier tier"}
    if "codex-auto-review" in ml or "luna" in ml:
        return {"in": 1.25, "cr": 0.125, "cw": 1.25, "out": 5.00, "note": "Fast/review tier estimate"}
    if ml == "raptor-mini" or (ml.startswith("gpt-") and ("mini" in ml or "nano" in ml)):
        return {"in": 0.40, "cr": 0.04, "cw": 0.40, "out": 1.60, "note": "GPT mini tier estimate"}
    if "grok-code" in ml:
        return {"in": 0.20, "cr": 0.05, "cw": 0.20, "out": 1.50, "note": "Grok Code Fast tier estimate"}
    if "grok" in ml:
        return {"in": 2.00, "cr": 0.50, "cw": 2.00, "out": 6.00, "note": "Grok tier estimate"}
    if "kimi" in ml:
        return {"in": 3.00, "cr": 0.30, "cw": 3.00, "out": 15.00, "note": "Kimi K3 tier estimate"}
    if ml.startswith("mai-"):
        return {"in": 0.20, "cr": 0.02, "cw": 0.20, "out": 1.20, "note": "MAI Flash tier estimate"}
    if "terra" in ml or "sol" in ml or "astra" in ml or ml.startswith("gpt-"):
        return {"in": 2.50, "cr": 0.25, "cw": 2.50, "out": 10.00, "note": "GPT frontier tier estimate"}
    if ml == "claude-(unknown)":
        return {"in": 2.00, "cr": 0.20, "cw": 2.50, "out": 10.00, "note": "Anthropic model (text-estimated tokens)"}
    if ml == "copilot-jetbrains":
        return {"in": 2.00, "cr": 0.20, "cw": 2.50, "out": 10.00, "note": "JetBrains Copilot (text-estimated tokens)"}
    if "gemini" in ml and "flash" in ml:
        return {"in": 0.30, "cr": 0.03, "cw": 0.30, "out": 2.50, "note": "Gemini Flash tier estimate"}
    if "gemini" in ml:
        return {"in": 2.00, "cr": 0.20, "cw": 2.00, "out": 12.00, "note": "Gemini Argon/Pro tier estimate"}
    return {"in": 2.00, "cr": 0.20, "cw": 2.50, "out": 10.00, "note": "Default frontier estimate"}


def load_billing_overrides() -> dict[str, Any]:
    if not BILLING_OVERRIDES_PATH.exists():
        return {}
    try:
        data = json.loads(BILLING_OVERRIDES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _monday_of_iso_date(date_str: str) -> str:
    try:
        d = dt.date.fromisoformat(date_str)
        return (d - dt.timedelta(days=d.weekday())).isoformat()
    except ValueError:
        return "unknown"


def _count_renewal_dates(start_iso: str, end_iso: str) -> list[str]:
    """Return all monthly billing renewal dates starting at start_iso up through end_iso."""
    try:
        sd = dt.date.fromisoformat(start_iso)
        ed = dt.date.fromisoformat(end_iso)
    except ValueError:
        return []
    if ed < sd:
        return [start_iso]
    out: list[str] = []
    cur_y, cur_m = sd.year, sd.month
    while True:
        next_y, next_m = (cur_y + 1, 1) if cur_m == 12 else (cur_y, cur_m + 1)
        last_dom = (dt.date(next_y, next_m, 1) - dt.timedelta(days=1)).day
        cyc = dt.date(cur_y, cur_m, min(sd.day, last_dom))
        if cyc > ed:
            break
        if cyc >= sd:
            out.append(cyc.isoformat())
        cur_y, cur_m = next_y, next_m
    return out or [start_iso]


def _count_annual_renewal_dates(start_iso: str, end_iso: str) -> tuple[list[str], str]:
    """Return `(annual_renewal_dates, cycle_end_iso)` starting at `start_iso` up through `end_iso`."""
    try:
        sd = dt.date.fromisoformat(start_iso)
        ed = dt.date.fromisoformat(end_iso)
    except ValueError:
        return ([start_iso], end_iso)
    if ed < sd:
        ed = sd
    out: list[str] = []
    cur_y = sd.year
    while True:
        last_dom = (dt.date(cur_y, 3, 1) - dt.timedelta(days=1)).day if sd.month == 2 else sd.day
        cyc = dt.date(cur_y, sd.month, min(sd.day, last_dom))
        if cyc > ed:
            next_start = cyc
            break
        out.append(cyc.isoformat())
        cur_y += 1
    return (out, (next_start - dt.timedelta(days=1)).isoformat())


def build_billing_summary(
    claude_res: dict[str, Any],
    codex_res: dict[str, Any],
    oc_res: dict[str, Any],
    copilot_res: dict[str, Any],
    cube_rows: list[list[Any]],
    default_rates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """
    Combine directly extracted billing/rate-limit telemetry, quantitative quota step-up
    inferences, and user-editable overrides into a unified Out-of-Pocket Billing Ledger.
    """
    c_meta = claude_res.get("billing_meta") or {}
    codex_wallet = codex_res.get("codex_wallet") or {}
    oc_bill = oc_res.get("opencode_billing") or {}
    cop_bill = copilot_res.get("copilot_billing") or {}

    # Analyze Claude Code weekly API-equivalent trajectory, burst days, and Fable overage volume
    claude_weekly_usd: dict[str, float] = defaultdict(float)
    claude_daily_usd: dict[str, float] = defaultdict(float)
    claude_fable_daily_usd: dict[str, float] = defaultdict(float)
    fable_calls = 0
    fable_tokens = 0
    fable_api_usd = 0.0
    min_claude_d, max_claude_d = "9999-99-99", "0000-00-00"
    min_ag_d, max_ag_d = "9999-99-99", "0000-00-00"
    min_cop_d, max_cop_d = "9999-99-99", "0000-00-00"

    for r in cube_rows:
        d_s, harness, _prov, model, _proj, _actor, _arch = r[:7]
        calls, _sess, inp, cr, cw, out_t, reas, _cost = r[7:15]
        if harness == "Antigravity" and d_s != "unknown":
            min_ag_d = min(min_ag_d, d_s)
            max_ag_d = max(max_ag_d, d_s)
        if harness == "GitHub Copilot" and d_s != "unknown":
            min_cop_d = min(min_cop_d, d_s)
            max_cop_d = max(max_cop_d, d_s)
        if harness != "Claude Code" or d_s == "unknown":
            continue
        min_claude_d = min(min_claude_d, d_s)
        max_claude_d = max(max_claude_d, d_s)
        rt = default_rates.get(model) or {"in": 2.0, "cr": 0.2, "cw": 2.5, "out": 10.0}
        est = (inp * rt["in"] + cr * rt["cr"] + cw * rt["cw"] + (out_t + reas) * rt["out"]) / 1e6
        wk = _monday_of_iso_date(d_s)
        if wk != "unknown":
            claude_weekly_usd[wk] += est
        claude_daily_usd[d_s] += est
        if "fable" in model.lower():
            fable_calls += calls
            fable_tokens += inp + cr + cw + out_t + reas
            fable_api_usd += est
            claude_fable_daily_usd[d_s] += est

    # Detect weekly step-up from Max 5x ($100/mo, ~$1.1K-$1.5K/wk API-equiv) to Max 20x ($200/mo, ~$4.5K/wk)
    sorted_weeks = sorted(claude_weekly_usd.items())
    inferred_upgrade_week = ""
    pre_jump_avg = 0.0
    post_jump_avg = 0.0
    for idx_w, (wk_d, wk_val) in enumerate(sorted_weeks):
        if wk_val >= 2500.0:
            inferred_upgrade_week = wk_d
            before_vals = [v for _, v in sorted_weeks[:idx_w] if v >= 200.0]
            after_vals = [v for _, v in sorted_weeks[idx_w:]]
            pre_jump_avg = round(sum(before_vals) / len(before_vals), 2) if before_vals else 0.0
            post_jump_avg = round(sum(after_vals) / len(after_vals), 2) if after_vals else 0.0
            break

    today_iso = dt.date.today().isoformat()
    cfg = load_local_config()
    has_claude_usage = min_claude_d <= max_claude_d or bool(c_meta.get("subscription_created_date"))
    sub_start = c_meta.get("subscription_created_date") or (
        min_claude_d if min_claude_d <= max_claude_d else today_iso
    )
    sub_end = max_claude_d if min_claude_d <= max_claude_d else today_iso
    renewals = _count_renewal_dates(sub_start, sub_end) if has_claude_usage else []
    upgrade_cutoff = inferred_upgrade_week or c_meta.get("fable_migration_date") or "9999-99-99"
    max5x_renewals = [d for d in renewals if d < upgrade_cutoff]
    max20x_renewals = [d for d in renewals if d >= upgrade_cutoff]

    # Compute per-day overage propensity weight for Claude Extra Usage attribution (bursts + Fable scoped limit)
    claude_overage_day_weights: dict[str, float] = {}
    for d_s, day_est in claude_daily_usd.items():
        daily_cap_est = 650.0 if d_s >= upgrade_cutoff else 180.0
        burst_excess = max(0.0, day_est - daily_cap_est)
        fable_w = claude_fable_daily_usd.get(d_s, 0.0) * 0.75
        w = burst_excess + fable_w
        if w > 1.0:
            claude_overage_day_weights[d_s] = round(w, 2)

    default_ledger: list[dict[str, Any]] = []
    aug_snap = c_meta.get("aug_snapshot") or {}

    # 1. Claude Subscriptions (Max 5x -> Max 20x)
    if max5x_renewals:
        end_5x = (
            (dt.date.fromisoformat(upgrade_cutoff) - dt.timedelta(days=1)).isoformat()
            if upgrade_cutoff != "9999-99-99"
            else sub_end
        )
        snap_clause = (
            f"cached snapshot ({aug_snap.get('date')}, `seven_day: {aug_snap.get('seven_day_pct')}%` at ~${pre_jump_avg:,.0f}/wk API-equiv) "
            if aug_snap
            else f"weekly usage (~${pre_jump_avg:,.0f}/wk API-equiv) "
        )
        default_ledger.append(
            {
                "id": "claude_sub_max5x",
                "harness": "Claude Code",
                "category": "subscription",
                "label": f"Claude Max 5x Plan ({len(max5x_renewals)} {'cycle' if len(max5x_renewals) == 1 else 'cycles'} @ $100/mo)",
                "start_date": sub_start,
                "end_date": max(sub_start, end_5x),
                "amount_usd": round(len(max5x_renewals) * 100.0, 2),
                "consumed_usd": round(len(max5x_renewals) * 100.0, 2),
                "remaining_usd": 0.0,
                "promo_value_usd": 0.0,
                "source": "inferred",
                "detail": (
                    f"Subscription started {sub_start} (`oauthAccount.subscriptionCreatedAt`); "
                    f"{snap_clause}matches Max 5x ($100/mo) across renewals {', '.join(max5x_renewals)}."
                ),
            }
        )
    if max20x_renewals:
        start_20x = inferred_upgrade_week or max20x_renewals[0]
        default_ledger.append(
            {
                "id": "claude_sub_max20x",
                "harness": "Claude Code",
                "category": "subscription",
                "label": f"Claude Max 20x Plan ({len(max20x_renewals)} {'cycle' if len(max20x_renewals) == 1 else 'cycles'} @ $200/mo)",
                "start_date": start_20x,
                "end_date": sub_end,
                "amount_usd": round(len(max20x_renewals) * 200.0, 2),
                "consumed_usd": round(len(max20x_renewals) * 200.0, 2),
                "remaining_usd": 0.0,
                "promo_value_usd": 0.0,
                "source": "inferred",
                "detail": (
                    f"Weekly usage jumped ~{(post_jump_avg / pre_jump_avg) if pre_jump_avg else 4.0:.1f}x starting week of {start_20x} "
                    f"(to ~${post_jump_avg:,.0f}/wk sustained API-equiv), matching Max 20x ($200/mo) across renewals {', '.join(max20x_renewals)}."
                ),
            }
        )

    # 2. Claude Extra Usage (Overages)
    if has_claude_usage and (c_meta.get("has_extra_usage_enabled") or aug_snap or cfg.get("claude_overage_default_usd") is not None):
        snap_note = (
            f"Cached snapshot ({aug_snap.get('date')}) logged ${aug_snap.get('used_credits_usd', 0):.2f} / "
            f"${aug_snap.get('monthly_limit_usd', 0):.0f} cap."
            if aug_snap
            else "Extra Usage enabled in `.claude.json`."
        )
        cfg_ov = cfg.get("claude_overage_default_usd")
        ov_default = float(cfg_ov) if cfg_ov is not None else float(aug_snap.get("used_credits_usd") or 0.0)
        ov_src = "user_calibrated" if cfg_ov is not None else ("extracted" if aug_snap else "inferred")
        default_ledger.append(
            {
                "id": "claude_overage",
                "harness": "Claude Code",
                "category": "overage",
                "label": "Claude Code Extra Usage (Overages)",
                "start_date": aug_snap.get("date") or sub_start,
                "end_date": sub_end,
                "amount_usd": round(ov_default, 2),
                "consumed_usd": round(ov_default, 2),
                "remaining_usd": 0.0,
                "promo_value_usd": 0.0,
                "source": ov_src,
                "detail": (
                    f"{snap_note} Newer CLI builds don't persist live Stripe Extra Usage totals locally; "
                    f"edit this line item to match your exact Stripe overage spend."
                ),
            }
        )

    # 3. Codex Subscriptions (directly extracted from `rate_limits.plan_type`)
    PLAN_LABELS = {
        "plus": "ChatGPT Plus",
        "prolite": "ChatGPT Pro Lite",
        "pro": "ChatGPT Pro",
        "team": "ChatGPT Team",
    }
    for idx_seg, seg in enumerate(codex_wallet.get("plan_segments") or []):
        pt = seg["plan_type"]
        p_lbl = PLAN_LABELS.get(pt, f"Codex {pt.capitalize()}")
        cyc = seg["cycles"]
        m_usd = seg["monthly_usd"]
        default_ledger.append(
            {
                "id": f"codex_sub_{pt}_{idx_seg}",
                "harness": "Codex",
                "category": "subscription",
                "label": f"{p_lbl} ({cyc} {'cycle' if cyc == 1 else 'cycles'} @ ${m_usd:.0f}/mo)",
                "start_date": seg["start_date"],
                "end_date": seg["end_date"],
                "amount_usd": seg["estimated_usd"],
                "consumed_usd": seg["estimated_usd"],
                "remaining_usd": 0.0,
                "promo_value_usd": 0.0,
                "source": "extracted",
                "detail": (
                    f"Extracted directly from Codex rollout `rate_limits.plan_type = \"{pt}\"` "
                    f"({seg['start_date']} .. {seg['end_date']})."
                ),
            }
        )

    # 4. Codex Credit Packs / Overages (100% exact from `rate_limits.credits.balance`)
    purchases = codex_wallet.get("purchases") or []
    if purchases or codex_wallet.get("overage_billed_usd", 0) > 0:
        p_dates = [p["date"] for p in purchases if p.get("date")]
        c_start = min(p_dates) if p_dates else (codex_wallet.get("first_sample_date") or today_iso)
        c_end = codex_wallet.get("last_sample_date") or max(p_dates or [today_iso])
        billed = float(codex_wallet.get("overage_billed_usd") or 0.0)
        burned = float(codex_wallet.get("overage_burned_usd") or 0.0)
        rem = float(codex_wallet.get("overage_remaining_usd") or 0.0)
        default_ledger.append(
            {
                "id": "codex_credits",
                "harness": "Codex",
                "category": "overage",
                "label": f"Codex Credit Packs ({len(purchases)} × 500 credits @ $20)",
                "start_date": c_start,
                "end_date": c_end,
                "amount_usd": billed,
                "consumed_usd": burned,
                "remaining_usd": rem,
                "promo_value_usd": 0.0,
                "source": "extracted",
                "detail": (
                    f"Exact wallet ledger from `rate_limits.credits.balance` ($0.04/credit): "
                    f"{codex_wallet.get('credits_purchased', 0):,.0f} cr (${billed:.2f}) purchased across {len(purchases)} reloads; "
                    f"{codex_wallet.get('credits_burned', 0):,.2f} cr (${burned:.2f}) burned; "
                    f"{codex_wallet.get('credits_remaining', 0):,.2f} cr (${rem:.2f}) still in wallet."
                ),
            }
        )

    # 5. OpenCode Direct API / Promotional Credits
    promo_usd = float(oc_bill.get("promo_anthropic_usd") or 0.0)
    hdr_promo_usd = float(oc_bill.get("session_header_promo_usd") or promo_usd)
    direct_oc_usd = float(oc_bill.get("direct_paid_usd") or 0.0)
    if promo_usd > 0:
        default_ledger.append(
            {
                "id": "opencode_promo",
                "harness": "OpenCode",
                "category": "promo_credit",
                "label": "OpenCode Promotional Credits (Free Promo)",
                "start_date": oc_bill.get("promo_start_date") or today_iso,
                "end_date": oc_bill.get("promo_end_date") or today_iso,
                "amount_usd": 0.0,
                "consumed_usd": 0.0,
                "remaining_usd": 0.0,
                "promo_value_usd": promo_usd,
                "source": "user_calibrated",
                "detail": (
                    f"Logged `opencode.db` spend (${promo_usd:.2f} across messages / ${hdr_promo_usd:.2f} session headers) "
                    f"covered by promotional credits ($0 cash paid). Local self-hosted GPU is $0."
                ),
            }
        )
    if direct_oc_usd > 0:
        default_ledger.append(
            {
                "id": "opencode_direct",
                "harness": "OpenCode",
                "category": "overage",
                "label": "OpenCode Logged Direct API Spend",
                "start_date": oc_bill.get("paid_start_date") or today_iso,
                "end_date": oc_bill.get("paid_end_date") or today_iso,
                "amount_usd": direct_oc_usd,
                "consumed_usd": direct_oc_usd,
                "remaining_usd": 0.0,
                "promo_value_usd": 0.0,
                "source": "extracted",
                "detail": f"Logged direct API spend in `opencode.db` (${direct_oc_usd:.2f}).",
            }
        )

    # 6. Antigravity Subscriptions
    if min_ag_d <= max_ag_d:
        ag_start = min_ag_d
        ag_end = max_ag_d
        ag_cfg = cfg.get("antigravity_plan") if isinstance(cfg.get("antigravity_plan"), dict) else None
        if ag_cfg and ag_cfg.get("paid_start"):
            ag_paid_start = str(ag_cfg["paid_start"])
            free_until = str(ag_cfg.get("free_until") or "")
            m_usd = float(ag_cfg.get("monthly_usd") or 0.0)
            if ag_start < ag_paid_start and free_until:
                default_ledger.append(
                    {
                        "id": "antigravity_sub_free",
                        "harness": "Antigravity",
                        "category": "subscription",
                        "label": "Antigravity Included Plan ($0/mo)",
                        "start_date": ag_start,
                        "end_date": free_until,
                        "amount_usd": 0.0,
                        "consumed_usd": 0.0,
                        "remaining_usd": 0.0,
                        "promo_value_usd": 0.0,
                        "source": "user_calibrated",
                        "detail": "Included $0 plan window prior to paid tier upgrade.",
                    }
                )
            ag_paid_end = max(ag_paid_start, ag_end, today_iso)
            ag_renewals = _count_renewal_dates(ag_paid_start, ag_paid_end) or [ag_paid_start]
            ag_paid_usd = round(len(ag_renewals) * m_usd, 2)
            default_ledger.append(
                {
                    "id": "antigravity_sub_paid",
                    "harness": "Antigravity",
                    "category": "subscription",
                    "label": f"Antigravity ${m_usd:.0f}/mo Plan ({len(ag_renewals)} {'cycle' if len(ag_renewals) == 1 else 'cycles'} @ ${m_usd:.0f}/mo)",
                    "start_date": ag_paid_start,
                    "end_date": ag_paid_end,
                    "amount_usd": ag_paid_usd,
                    "consumed_usd": ag_paid_usd,
                    "remaining_usd": 0.0,
                    "promo_value_usd": 0.0,
                    "source": "user_calibrated",
                    "detail": f"Configured ${m_usd:.0f}/mo Antigravity plan starting {ag_paid_start} ({', '.join(ag_renewals)}).",
                }
            )
        else:
            default_ledger.append(
                {
                    "id": "antigravity_sub",
                    "harness": "Antigravity",
                    "category": "subscription",
                    "label": "Antigravity Plan / Quota",
                    "start_date": ag_start,
                    "end_date": ag_end,
                    "amount_usd": 0.0,
                    "consumed_usd": 0.0,
                    "remaining_usd": 0.0,
                    "promo_value_usd": 0.0,
                    "source": "extracted",
                    "detail": "No local overage charges logged in trajectory DBs; edit to attribute a monthly plan.",
                }
            )

    # 7. GitHub Copilot Subscriptions & Premium Request Overages (only emitted when Copilot usage exists)
    if min_cop_d <= max_cop_d:
        cop_start = min_cop_d
        cop_end = max_cop_d
        prem_reqs = float(cop_bill.get("premium_requests") or 0.0)
        cop_cfg = cfg.get("copilot_plan") if isinstance(cfg.get("copilot_plan"), dict) else None
        if cop_cfg and cop_cfg.get("start_date"):
            c_paid_start = str(cop_cfg["start_date"])
            c_monthly = float(cop_cfg.get("monthly_usd") or 10.0)
            c_paid_end = max(c_paid_start, cop_end, today_iso)
            c_renewals = _count_renewal_dates(c_paid_start, c_paid_end) or [c_paid_start]
            c_paid_usd = round(len(c_renewals) * c_monthly, 2)
            default_ledger.append(
                {
                    "id": "copilot_sub_paid",
                    "harness": "GitHub Copilot",
                    "category": "subscription",
                    "label": f"GitHub Copilot Plan ({len(c_renewals)} {'cycle' if len(c_renewals) == 1 else 'cycles'} @ ${c_monthly:.0f}/mo)",
                    "start_date": c_paid_start,
                    "end_date": c_paid_end,
                    "amount_usd": c_paid_usd,
                    "consumed_usd": c_paid_usd,
                    "remaining_usd": 0.0,
                    "promo_value_usd": 0.0,
                    "source": "user_calibrated",
                    "detail": (
                        f"Configured ${c_monthly:.0f}/mo GitHub Copilot plan starting {c_paid_start} "
                        f"({prem_reqs:,.1f} premium requests logged)."
                    ),
                }
            )
        else:
            sku_segs = cop_bill.get("sku_segments") or []
            by_sku = {str(s["sku"]): s for s in sku_segs if isinstance(s, dict) and s.get("sku")}
            trial_seg = by_sku.get("trial_30_yearly_subscriber_quota") or by_sku.get("trial_30_monthly_subscriber_quota")
            yearly_seg = by_sku.get("yearly_subscriber_quota")
            monthly_seg = by_sku.get("monthly_subscriber_quota")
            plus_seg = by_sku.get("plus_monthly_subscriber_quota") or by_sku.get("pro_plus_monthly_subscriber_quota")
            emitted_cop_sku = False
            trial_end_after = ""

            if trial_seg:
                t_start = str(trial_seg.get("first_date") or cop_start)
                try:
                    t_sd = dt.date.fromisoformat(t_start)
                    t_end = (t_sd + dt.timedelta(days=29)).isoformat()
                    trial_end_after = (t_sd + dt.timedelta(days=30)).isoformat()
                except ValueError:
                    t_end = str(trial_seg.get("last_date") or t_start)
                    trial_end_after = t_end
                default_ledger.append(
                    {
                        "id": "copilot_sub_trial",
                        "harness": "GitHub Copilot",
                        "category": "subscription",
                        "label": "GitHub Copilot Pro (30-Day Free Trial)",
                        "start_date": t_start,
                        "end_date": t_end,
                        "amount_usd": 0.0,
                        "consumed_usd": 0.0,
                        "remaining_usd": 0.0,
                        "promo_value_usd": 0.0,
                        "source": "extracted",
                        "detail": (
                            f"Extracted from `GitHub Copilot Chat.log` (`sku: {trial_seg['sku']}`, "
                            f"{trial_seg.get('first_date')} .. {trial_seg.get('last_date')}); $0 30-day free trial."
                        ),
                    }
                )
                emitted_cop_sku = True

            if yearly_seg:
                y_start = trial_end_after or str(yearly_seg.get("first_date") or cop_start)
                y_horizon = max(cop_end, str(yearly_seg.get("last_date") or cop_end))
                y_renewals, y_cycle_end = _count_annual_renewal_dates(y_start, y_horizon)
                y_usd = round(len(y_renewals) * 100.0, 2)
                default_ledger.append(
                    {
                        "id": "copilot_sub_yearly",
                        "harness": "GitHub Copilot",
                        "category": "subscription",
                        "label": f"GitHub Copilot Pro Annual Plan ({len(y_renewals)} {'yr' if len(y_renewals) == 1 else 'yrs'} @ $100/yr)",
                        "start_date": y_start,
                        "end_date": y_cycle_end,
                        "amount_usd": y_usd,
                        "consumed_usd": y_usd,
                        "remaining_usd": 0.0,
                        "promo_value_usd": 0.0,
                        "source": "inferred",
                        "detail": (
                            f"Inferred from `GitHub Copilot Chat.log` (`sku: yearly_subscriber_quota`, "
                            f"{yearly_seg.get('first_date')} .. {yearly_seg.get('last_date')}); "
                            f"{prem_reqs:,.1f} premium requests logged."
                        ),
                    }
                )
                emitted_cop_sku = True
            elif plus_seg or monthly_seg:
                m_seg = plus_seg or monthly_seg
                m_rate = 39.0 if plus_seg else 10.0
                m_tier = "Pro+" if plus_seg else "Pro"
                m_start = trial_end_after or str(m_seg.get("first_date") or cop_start)
                m_end = max(cop_end, str(m_seg.get("last_date") or cop_end))
                m_renewals = _count_renewal_dates(m_start, m_end) or [m_start]
                m_usd = round(len(m_renewals) * m_rate, 2)
                default_ledger.append(
                    {
                        "id": "copilot_sub_monthly",
                        "harness": "GitHub Copilot",
                        "category": "subscription",
                        "label": f"GitHub Copilot {m_tier} ({len(m_renewals)} {'cycle' if len(m_renewals) == 1 else 'cycles'} @ ${m_rate:.0f}/mo)",
                        "start_date": m_start,
                        "end_date": m_end,
                        "amount_usd": m_usd,
                        "consumed_usd": m_usd,
                        "remaining_usd": 0.0,
                        "promo_value_usd": 0.0,
                        "source": "inferred",
                        "detail": (
                            f"Inferred from `GitHub Copilot Chat.log` (`sku: {m_seg['sku']}`, "
                            f"{m_seg.get('first_date')} .. {m_seg.get('last_date')}); "
                            f"{prem_reqs:,.1f} premium requests logged."
                        ),
                    }
                )
                emitted_cop_sku = True

            if not emitted_cop_sku:
                default_ledger.append(
                    {
                        "id": "copilot_sub",
                        "harness": "GitHub Copilot",
                        "category": "subscription",
                        "label": "GitHub Copilot Plan / Premium Requests",
                        "start_date": cop_start,
                        "end_date": cop_end,
                        "amount_usd": 0.0,
                        "consumed_usd": 0.0,
                        "remaining_usd": 0.0,
                        "promo_value_usd": 0.0,
                        "source": "extracted",
                        "detail": (
                            f"Logged {prem_reqs:,.1f} premium requests across Copilot sessions "
                            f"(Pro $10/mo or $100/yr, Pro+ $39/mo, overages @ $0.04/premium request); edit to attribute your plan."
                        ),
                    }
                )

    saved_overrides = load_billing_overrides()

    return {
        "default_ledger": default_ledger,
        "saved_overrides": saved_overrides,
        "codex_wallet": codex_wallet,
        "claude_telemetry": {
            **c_meta,
            "inferred_upgrade_week": inferred_upgrade_week,
            "pre_upgrade_weekly_api_usd": pre_jump_avg,
            "post_upgrade_weekly_api_usd": post_jump_avg,
            "fable_calls": fable_calls,
            "fable_tokens": fable_tokens,
            "fable_api_equiv_usd": round(fable_api_usd, 2),
            "weekly_api_usd": {k: round(v, 2) for k, v in sorted_weeks},
            "overage_day_weights": claude_overage_day_weights,
        },
        "opencode_telemetry": oc_bill,
        "copilot_telemetry": cop_bill,
    }


def _session_detail_id(harness: str, root_id: str) -> str:
    return hashlib.sha1(f"{harness}\x00{root_id}".encode("utf-8")).hexdigest()[:16]


def build_session_details(
    results: list[dict[str, Any]],
    all_sessions: list[dict[str, Any]],
    default_rates: dict[str, Any],
) -> dict[str, Any]:
    """
    Group every session with its subagent / forked / auto-review descendants and write one
    `web/sessions/<id>.js` per group (a per-call timeline the session page loads via <script>, so
    it also works over file://). Sessions get a `detail` id pointing at their group's file.
    Unchanged groups are not rewritten; files for groups that disappeared are removed.
    """
    sess_by_key: dict[tuple[str, str], dict[str, Any]] = {(s["harness"], s["id"]): s for s in all_sessions}
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    tl_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for res in results:
        tls = res.get("timelines") or {}
        harness = (res.get("health") or {}).get("harness") or ""
        known = set(tls.keys())

        def root_of(sid: str) -> str:
            seen = {sid}
            cur = sid
            while True:
                par = (tls.get(cur) or {}).get("parent") or ""
                if not par or par in seen or (par not in known and (harness, par) not in sess_by_key):
                    return cur
                seen.add(par)
                cur = par

        for sid, tl in tls.items():
            if not tl.get("calls"):
                continue
            tl_by_key[(harness, sid)] = tl
            groups[(harness, root_of(sid))].append(sid)

    SESSION_DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        manifest = json.loads(SESSION_DETAIL_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    new_manifest: dict[str, str] = {}
    written = 0
    total_calls = 0

    for (harness, root), members in sorted(groups.items()):
        detail_id = _session_detail_id(harness, root)
        calls_all: list[tuple[int, int, list[Any]]] = []
        agent_first: dict[str, int] = {}
        untimed = 0
        for sid in members:
            for c in tl_by_key[(harness, sid)]["calls"]:
                if c[0] > 0:
                    calls_all.append((c[0], 0, [sid, *c]))
                    agent_first[sid] = min(agent_first.get(sid, c[0]), c[0])
                else:
                    untimed += 1
        if not calls_all:
            continue
        # Root first, then descendants in order of their first call
        order = sorted(members, key=lambda m: (m != root, agent_first.get(m, 1 << 62), m))
        order = [m for m in order if m in agent_first]
        agent_idx = {sid: i for i, sid in enumerate(order)}
        calls_all.sort(key=lambda t: (t[0], agent_idx[t[2][0]]))
        t0 = calls_all[0][0]
        models: list[str] = []
        model_idx: dict[str, int] = {}
        efforts: list[str] = []
        effort_idx: dict[str, int] = {}
        rows = []
        for ms, _o, (sid, _ms, model, inp, cr, cw, out_txt, reas, cost, *rest) in calls_all:
            effort = str(rest[0]) if rest else ""
            if model not in model_idx:
                model_idx[model] = len(models)
                models.append(model)
            if effort not in effort_idx:
                effort_idx[effort] = len(efforts)
                efforts.append(effort)
            rows.append(
                [ms - t0, agent_idx[sid], model_idx[model], inp, cr, cw, out_txt, reas, round(float(cost), 6), effort_idx[effort]]
            )
        total_calls += len(rows)

        agents = []
        for sid in order:
            s = sess_by_key.get((harness, sid)) or {}
            tl = tl_by_key[(harness, sid)]
            agents.append(
                {
                    "id": sid,
                    "parent": tl.get("parent") or "",
                    "title": s.get("title") or sid,
                    "actor": s.get("actor") or ("main" if sid == root else "subagent"),
                    "model": s.get("model") or "",
                    "project": s.get("project") or "",
                }
            )
        root_s = sess_by_key.get((harness, root)) or {}
        detail = {
            "id": detail_id,
            "harness": harness,
            "root": root,
            "title": root_s.get("title") or root,
            "project": root_s.get("project") or (agents[0]["project"] if agents else ""),
            "t0": t0,
            "untimed_calls": untimed,
            "models": models,
            "efforts": efforts,
            "rates": {m: default_rates.get(m) for m in models if default_rates.get(m)},
            "agents": agents,
            "call_columns": [
                "dt_ms", "agent", "model", "input_uncached", "cache_read", "cache_write",
                "output_text", "reasoning", "logged_cost_usd", "effort",
            ],
            "calls": rows,
        }
        body = json.dumps(detail, separators=(",", ":"))
        digest = hashlib.sha1(body.encode("utf-8")).hexdigest()
        new_manifest[detail_id] = digest
        out_path = SESSION_DETAIL_DIR / f"{detail_id}.js"
        if manifest.get(detail_id) != digest or not out_path.exists():
            _atomic_write_text(out_path, f"window.__TOKEN_SESSION__ = {body};\n")
            written += 1
        for sid in members:
            s = sess_by_key.get((harness, sid))
            if s is not None:
                s["detail"] = detail_id

    removed = 0
    for f in SESSION_DETAIL_DIR.glob("*.js"):
        if f.stem not in new_manifest:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    _atomic_write_text(SESSION_DETAIL_MANIFEST_PATH, json.dumps(new_manifest, separators=(",", ":")))
    return {"groups": len(new_manifest), "written": written, "removed": removed, "calls": total_calls}


def extract_all(rebuild: bool = False) -> dict[str, Any]:
    with EXTRACT_LOCK:
        return _extract_all_locked(rebuild=rebuild)


def _extract_all_locked(rebuild: bool = False) -> dict[str, Any]:
    t_start = time.time()
    cache_con = open_cache_db(rebuild=rebuild)
    workers = min(16, max(2, os.cpu_count() or 4))

    # Use forkserver (multi-thread safe inside ThreadingHTTPServer) when imported as a .py module file
    real_self = str(Path(__file__).resolve())
    main_mod = sys.modules.get("__main__")
    if main_mod is not None and getattr(main_mod, "__file__", None) and not str(main_mod.__file__).endswith(".py"):
        main_mod.__file__ = real_self
    can_forkserver = (
        bool(getattr(main_mod, "__file__", None))
        and str(getattr(main_mod, "__file__", "")).endswith(".py")
        and os.path.exists(getattr(main_mod, "__file__", ""))
    )
    mp_method = "forkserver" if can_forkserver else "fork"
    mp_ctx = multiprocessing.get_context(mp_method)

    with ProcessPoolExecutor(max_workers=workers, mp_context=mp_ctx) as pool:
        claude_res = extract_claude(cache_con, pool)
        codex_res = extract_codex(cache_con, pool)
        ag_res = extract_antigravity(cache_con, pool)
        copilot_res = extract_copilot(cache_con, pool)

    oc_res = extract_opencode(cache_con)
    cache_con.close()

    merged_cube: dict[tuple[str, str, str, str, str, str, int], list[float]] = defaultdict(
        lambda: [0, 0, 0, 0, 0, 0, 0.0]
    )
    all_sessions: list[dict[str, Any]] = []
    merged_proj_paths: dict[str, set[str]] = defaultdict(set)
    health_list: list[dict[str, Any]] = []

    # Extractors key cube cells by a local "YYYY-MM-DDTHH" bucket (or a bare date when the hour is
    # unknown). Roll those up into the daily cube and keep a parallel per-hour breakdown of each cell.
    hourly_acc: dict[tuple[tuple[str, str, str, str, str, str, int], int], list[float]] = defaultdict(
        lambda: [0, 0, 0, 0, 0, 0, 0.0]
    )
    for res in (claude_res, codex_res, oc_res, ag_res, copilot_res):
        for k, vals in res["cube"].items():
            date_s, hour = split_hour_key(k[0])
            day_k = (date_s, *k[1:])
            b = merged_cube[day_k]
            for i in range(7):
                b[i] += vals[i]
            if hour >= 0:
                hb = hourly_acc[(day_k, hour)]
                for i in range(7):
                    hb[i] += vals[i]
        all_sessions.extend(res["sessions"])
        for proj, paths in res["project_paths"].items():
            merged_proj_paths[proj].update(paths)
        if res.get("health"):
            health_list.append(res["health"])

    sess_counts_by_cell: dict[tuple[str, str, str, str, str, str, int], int] = defaultdict(int)
    for s in all_sessions:
        ck = (s["date"], s["harness"], s["provider"], s["model"], s["project"], s["actor"], 0)
        sess_counts_by_cell[ck] += 1

    cube_rows = []
    cube_row_index: dict[tuple[str, str, str, str, str, str, int], int] = {}
    distinct_models: dict[str, str] = {}
    for k in sorted(merged_cube.keys()):
        date_s, harness, provider, model, project, actor, archived = k
        calls, inp, cr, cw, out_txt, reas, cost = merged_cube[k]
        if (inp + cr + cw + out_txt + reas) == 0 and cost == 0:
            continue
        distinct_models[model] = provider
        cube_row_index[k] = len(cube_rows)
        cube_rows.append(
            [
                date_s,
                harness,
                provider,
                model,
                project,
                actor,
                archived,
                int(calls),
                int(sess_counts_by_cell.get(k, 0)),
                int(inp),
                int(cr),
                int(cw),
                int(out_txt),
                int(reas),
                round(float(cost), 6),
            ]
        )

    # Compact hourly breakdown: each row points back at its daily cube row (dimensions live there)
    cube_hourly_rows = []
    for (day_k, hour) in sorted(hourly_acc.keys(), key=lambda hk: (cube_row_index.get(hk[0], -1), hk[1])):
        row_idx = cube_row_index.get(day_k)
        if row_idx is None:
            continue
        calls, inp, cr, cw, out_txt, reas, cost = hourly_acc[(day_k, hour)]
        if (inp + cr + cw + out_txt + reas) == 0 and cost == 0:
            continue
        cube_hourly_rows.append(
            [row_idx, hour, int(calls), int(inp), int(cr), int(cw), int(out_txt), int(reas), round(float(cost), 6)]
        )

    # Align active_tokens with frontend (fresh prompt input + cache write + output + reasoning)
    for s in all_sessions:
        s["total_tokens"] = (
            s["input_uncached"] + s["cache_read"] + s["cache_write"] + s["output_text"] + s["reasoning"]
        )
        s["active_tokens"] = (
            s["input_uncached"] + s["cache_write"] + s["output_text"] + s["reasoning"]
        )

    # Export all non-empty sessions sorted by total_tokens so every filtered view has 100% session coverage
    ranked_sessions = sorted(all_sessions, key=lambda x: x["total_tokens"], reverse=True)

    default_rates = {
        m: default_model_pricing(m, prov) for m, prov in sorted(distinct_models.items())
    }
    billing_summary = build_billing_summary(claude_res, codex_res, oc_res, copilot_res, cube_rows, default_rates)
    session_detail_stats = build_session_details(
        [claude_res, codex_res, oc_res, ag_res, copilot_res], all_sessions, default_rates
    )

    total_ms = round((time.time() - t_start) * 1000, 1)
    payload = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "hostname": os.uname().nodename,
        "extraction_ms": total_ms,
        "session_details": session_detail_stats,
        "total_sessions_indexed": len(all_sessions),
        "cube_columns": [
            "date",
            "harness",
            "provider",
            "model",
            "project",
            "actor",
            "archived",
            "calls",
            "sessions",
            "input_uncached",
            "cache_read",
            "cache_write",
            "output_text",
            "reasoning",
            "logged_cost_usd",
        ],
        "cube": cube_rows,
        "cube_hourly_columns": [
            "cube_row",
            "hour",
            "calls",
            "input_uncached",
            "cache_read",
            "cache_write",
            "output_text",
            "reasoning",
            "logged_cost_usd",
        ],
        "cube_hourly": cube_hourly_rows,
        "top_sessions": ranked_sessions,
        "default_rates_per_1m": default_rates,
        "billing": billing_summary,
        "project_paths": {k: sorted(v) for k, v in sorted(merged_proj_paths.items())},
        "source_health": health_list,
    }

    json_str = json.dumps(payload, separators=(",", ":"))
    _atomic_write_text(STATS_JSON_PATH, json_str)
    _atomic_write_text(WEB_DATA_JS_PATH, f"window.__TOKEN_STATS__ = {json_str};\n")
    return payload


# ---------------------------------------------------------------------------
# CLI Terminal Summary
# ---------------------------------------------------------------------------


def fmt_tok(n: float | int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:7.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:7.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:7.1f}K"
    return f"{int(n):8d}"


def print_summary(payload: dict[str, Any]) -> None:
    rows = payload["cube"]
    print(f"\n=== Local Token Observatory ({payload['hostname']}) — extracted in {payload['extraction_ms']} ms ===")
    print(f"Generated at: {payload['generated_at']} | Total sessions indexed: {payload['total_sessions_indexed']:,}\n")

    for h in payload["source_health"]:
        print(
            f"  • {h['harness']:<14} | sessions={h.get('sessions_count', 0):>5,} | calls={h.get('api_calls', 0):>7,} "
            f"| sources={h.get('files_total', 0):>4} (cache={h.get('cache_hits', 0):>4}) | {h.get('elapsed_ms', 0):>6.1f} ms | {h.get('live_date_range', '')}"
        )

    def group_print(title: str, col_idx: int, limit: int = 15) -> None:
        acc: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0.0])
        for r in rows:
            g = str(r[col_idx])
            b = acc[g]
            b[0] += r[7]   # calls
            b[1] += r[9]   # input_uncached
            b[2] += r[10]  # cache_read
            b[3] += r[11]  # cache_write
            b[4] += r[12]  # output_text
            b[5] += r[13]  # reasoning
            b[6] += r[14]  # cost
        ranked = sorted(acc.items(), key=lambda kv: sum(kv[1][1:6]), reverse=True)
        grand = sum(sum(v[1:6]) for _, v in ranked) or 1
        print(f"\n--- {title} ---")
        print(
            f"{'Name':<36} {'Total':>9} {'Share':>6} {'Uncached In':>11} {'Cache Read':>11} {'Cache Write':>11} {'Output':>9} {'Reason':>9} {'Cache%':>7} {'Calls':>8}"
        )
        print("-" * 126)
        for name, v in ranked[:limit]:
            tot = sum(v[1:6])
            tot_in = v[1] + v[2] + v[3]
            ch = (v[2] / tot_in * 100.0) if tot_in > 0 else 0.0
            print(
                f"{name[:36]:<36} {fmt_tok(tot):>9} {tot / grand * 100:5.1f}% "
                f"{fmt_tok(v[1]):>11} {fmt_tok(v[2]):>11} {fmt_tok(v[3]):>11} "
                f"{fmt_tok(v[4]):>9} {fmt_tok(v[5]):>9} {ch:6.1f}% {int(v[0]):>8,}"
            )

    group_print("By Harness", 1)
    group_print("By Model (Top 20)", 3, limit=20)
    group_print("By Project", 4, limit=15)
    group_print("By Actor Role", 5)

    billing = payload.get("billing") or {}
    ledger = billing.get("default_ledger") or []
    overrides = (billing.get("saved_overrides") or {}).get("items") or {}
    if ledger:
        print("\n--- Actual Out-of-Pocket Spend (Subscriptions + Overages, Net of Promo Credits) ---")
        print(f"{'Harness':<14} {'Category':<14} {'Source':<15} {'Span':<24} {'Paid ($)':>10} {'Notes / Item'}")
        print("-" * 126)
        tot_sub = tot_ov = tot_promo = 0.0
        for it in ledger:
            ov_it = overrides.get(it["id"]) or {}
            if ov_it.get("enabled") is False:
                continue
            amt = float(ov_it.get("amount_usd", it["amount_usd"]))
            cat = it["category"]
            if cat == "subscription":
                tot_sub += amt
            elif cat == "overage":
                tot_ov += amt
            elif cat == "promo_credit":
                tot_promo += float(it.get("promo_value_usd") or 0.0)
            span = f"{it['start_date']}..{it['end_date']}"
            lbl = ov_it.get("label") or it["label"]
            print(
                f"{it['harness']:<14} {cat:<14} {it['source']:<15} {span:<24} ${amt:>9.2f}  {lbl}"
            )
        for c_it in (billing.get("saved_overrides") or {}).get("custom_items") or []:
            if c_it.get("enabled") is False:
                continue
            amt = float(c_it.get("amount_usd") or 0.0)
            cat = str(c_it.get("category") or "overage")
            if cat == "subscription":
                tot_sub += amt
            else:
                tot_ov += amt
            span = f"{c_it.get('start_date', '')}..{c_it.get('end_date', '')}"
            print(
                f"{str(c_it.get('harness', 'Other')):<14} {cat:<14} {'custom':<15} {span:<24} ${amt:>9.2f}  {c_it.get('label', 'Custom item')}"
            )
        print("-" * 126)
        print(
            f"TOTAL ACTUAL PAID: ${tot_sub + tot_ov:,.2f}  "
            f"(Subscriptions: ${tot_sub:,.2f} | Overages/Credits: ${tot_ov:,.2f} | Free Promo Absorbed: ${tot_promo:,.2f})"
        )
    print()


# ---------------------------------------------------------------------------
# Lightweight Local HTTP Server (Thread-Safe + Localhost CSRF Guard)
# ---------------------------------------------------------------------------

ALLOWED_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class ObservatoryHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        try:
            sys.stderr.write(f"[http] {self.address_string()} - {fmt % args}\n")
            sys.stderr.flush()
        except OSError:
            # Never drop HTTP responses when stdout/stderr belongs to a closed PTY or pipe
            pass

    def _is_trusted_local_post(self) -> bool:
        host_hdr = (self.headers.get("Host") or "").split(":", 1)[0].strip().lower()
        bind_host = str(self.server.server_address[0]).lower()
        allowed = ALLOWED_LOCAL_HOSTS | {bind_host}
        if host_hdr not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin:
            o_host = (urlparse(origin).hostname or "").lower()
            if o_host not in allowed:
                return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            body = json.dumps({"ok": True, "app": "token-stats", "pid": os.getpid()}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/stats":
            if not STATS_JSON_PATH.exists():
                extract_all(rebuild=False)
            data = STATS_JSON_PATH.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/api/refresh", "/api/rebuild"):
            if not self._is_trusted_local_post():
                self.send_error(HTTPStatus.FORBIDDEN, "Cross-origin POST rejected")
                return
            rebuild = parsed.path == "/api/rebuild"
            payload = extract_all(rebuild=rebuild)
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/billing":
            if not self._is_trusted_local_post():
                self.send_error(HTTPStatus.FORBIDDEN, "Cross-origin POST rejected")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0 or length > 256_000:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Invalid payload size")
                    return
                body = self.rfile.read(length) if length > 0 else b"{}"
                overrides = json.loads(body.decode("utf-8"))
                if not isinstance(overrides, dict):
                    raise ValueError("Expected JSON object")
                with EXTRACT_LOCK:
                    if not overrides.get("items") and not overrides.get("custom_items"):
                        if BILLING_OVERRIDES_PATH.exists():
                            BILLING_OVERRIDES_PATH.unlink()
                        clean_ov: dict[str, Any] = {}
                    else:
                        clean_ov = {
                            "items": overrides.get("items") or {},
                            "custom_items": overrides.get("custom_items") or [],
                        }
                        _atomic_write_text(BILLING_OVERRIDES_PATH, json.dumps(clean_ov, indent=2))
                    if STATS_JSON_PATH.exists():
                        cur_payload = json.loads(STATS_JSON_PATH.read_text(encoding="utf-8"))
                        if "billing" in cur_payload and isinstance(cur_payload["billing"], dict):
                            cur_payload["billing"]["saved_overrides"] = clean_ov
                            j_str = json.dumps(cur_payload, separators=(",", ":"))
                            _atomic_write_text(STATS_JSON_PATH, j_str)
                            _atomic_write_text(WEB_DATA_JS_PATH, f"window.__TOKEN_STATS__ = {j_str};\n")
                resp = json.dumps({"ok": True, "saved_overrides": clean_ov}).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, f"Invalid billing JSON: {exc}")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")


class ReuseThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, socket.timeout)):
            return
        try:
            sys.stderr.write(f"[http-error] {client_address}: {traceback.format_exc()}\n")
            sys.stderr.flush()
        except OSError:
            pass


def _probe_existing_observatory(host: str, port: int, timeout: float = 1.5) -> bool:
    """Return True if a healthy Token Observatory HTTP server is already responding on host:port."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    base = f"http://{probe_host}:{port}"
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
                if isinstance(data, dict) and data.get("app") == "token-stats":
                    return True
    except Exception:
        pass
    try:
        with urllib.request.urlopen(f"{base}/", timeout=timeout) as resp:
            if resp.status == 200:
                html = resp.read(4096).decode("utf-8", "ignore")
                if "Local AI Token" in html:
                    return True
    except Exception:
        pass
    return False


def _terminate_stale_self_listeners(port: int) -> int:
    """Terminate stale/wedged local `token_stats.py` processes owned by the current user."""
    my_pid = os.getpid()
    my_uid = os.getuid()
    candidate_pids: set[int] = set()

    if SERVER_PID_PATH.exists():
        try:
            info = json.loads(SERVER_PID_PATH.read_text(encoding="utf-8"))
            if isinstance(info, dict) and int(info.get("port") or 0) == port:
                pid_val = int(info.get("pid") or 0)
                if pid_val > 1 and pid_val != my_pid:
                    candidate_pids.add(pid_val)
        except Exception:
            pass

    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid_val = int(entry.name)
            if pid_val <= 1 or pid_val == my_pid:
                continue
            try:
                if entry.stat().st_uid != my_uid:
                    continue
                cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
            except OSError:
                continue
            if "token_stats.py" in cmdline and "multiprocessing.forkserver" not in cmdline:
                candidate_pids.add(pid_val)

    killed = 0
    for pid_val in candidate_pids:
        try:
            cmdline = Path(f"/proc/{pid_val}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
            if "token_stats.py" not in cmdline:
                continue
            os.kill(pid_val, signal.SIGTERM)
            killed += 1
        except OSError:
            continue

    if killed:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not any(Path(f"/proc/{p}").exists() for p in candidate_pids):
                break
            time.sleep(0.05)
    return killed


def run_server(
    host: str,
    port: int,
    rebuild: bool = False,
    open_browser: bool = False,
    quiet: bool = False,
) -> None:
    payload = extract_all(rebuild=rebuild)
    if not quiet:
        try:
            print_summary(payload)
        except OSError:
            pass

    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{probe_host}:{port}"

    server: ReuseThreadingHTTPServer | None = None
    try:
        server = ReuseThreadingHTTPServer((host, port), ObservatoryHandler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        # Port is already bound: either reuse a healthy running Token Observatory instance
        # or replace an orphaned/wedged `token_stats.py` listener whose terminal closed.
        if _probe_existing_observatory(host, port):
            print(f"Token Observatory is already running at {url} (logs refreshed).")
            if open_browser:
                webbrowser.open(url)
            return
        replaced = _terminate_stale_self_listeners(port)
        if replaced:
            try:
                server = ReuseThreadingHTTPServer((host, port), ObservatoryHandler)
                print(f"Replaced {replaced} stale token-stats background listener(s) on port {port}.")
            except OSError as retry_exc:
                if retry_exc.errno != errno.EADDRINUSE:
                    raise
        if server is None:
            print(
                f"Error: {url} is already in use by another process. "
                f"Pass `--port <PORT>` or stop the process using port {port}.",
                file=sys.stderr,
            )
            sys.exit(1)

    try:
        _atomic_write_text(
            SERVER_PID_PATH,
            json.dumps({"pid": os.getpid(), "host": host, "port": port}),
        )
    except OSError:
        pass

    print(f"Serving Token Observatory at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        try:
            print("\nStopping server.")
        except OSError:
            pass
    finally:
        server.server_close()
        try:
            if SERVER_PID_PATH.exists():
                cur = json.loads(SERVER_PID_PATH.read_text(encoding="utf-8"))
                if isinstance(cur, dict) and cur.get("pid") == os.getpid():
                    SERVER_PID_PATH.unlink()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="token-stats",
        description="Local AI Token Spend & Usage Observatory (defaults to `serve` when called with no subcommand)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host when serving (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port when serving (default: 8765)")
    parser.add_argument("-o", "--open", action="store_true", help="Open dashboard in browser after starting server")
    parser.add_argument("--rebuild", action="store_true", help="Ignore incremental cache and re-parse all logs")
    parser.add_argument("--quiet", action="store_true", help="Suppress terminal summary table output")

    sub = parser.add_subparsers(dest="cmd")

    p_srv = sub.add_parser("serve", help="Extract and start local web server with live Refresh button (default)")
    p_srv.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_srv.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    p_srv.add_argument("-o", "--open", action="store_true", help="Open dashboard in browser after starting server")
    p_srv.add_argument("--rebuild", action="store_true", help="Force full re-parse on startup")
    p_srv.add_argument("--quiet", action="store_true", help="Suppress terminal summary table on startup")

    p_sum = sub.add_parser("summary", help="Extract (incrementally) and print terminal breakdown tables without serving")
    p_sum.add_argument("--rebuild", action="store_true", help="Force full re-parse")

    p_ext = sub.add_parser("extract", help="Extract local logs into data/stats.json and web/data.js")
    p_ext.add_argument("--rebuild", action="store_true", help="Ignore incremental cache and re-parse all logs")
    p_ext.add_argument("--quiet", action="store_true", help="Suppress summary table output")

    args = parser.parse_args()
    cmd = args.cmd or "serve"

    if cmd in ("extract", "summary"):
        payload = extract_all(rebuild=getattr(args, "rebuild", False))
        if cmd == "summary" or not getattr(args, "quiet", False):
            print_summary(payload)
    elif cmd == "serve":
        run_server(
            args.host,
            args.port,
            rebuild=getattr(args, "rebuild", False),
            open_browser=getattr(args, "open", False),
            quiet=getattr(args, "quiet", False),
        )


if __name__ == "__main__":
    main()
