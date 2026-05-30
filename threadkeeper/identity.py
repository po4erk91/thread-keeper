"""Per-process session bookkeeping and self-conversation-id detection.
One MCP server process = one session row + a cached cid for whoami()."""
from __future__ import annotations

import os
import re
import sqlite3
import secrets
import subprocess
import time
from typing import Optional

from .config import CLIENT_LABEL, SELF_CID_TTL_S, CLAUDE_PROJECTS_DIR, WRITE_ORIGIN

# ──────────────────────────────────────────────────────────────────────────────
# Session tracking. One MCP server process = one Claude Desktop window.
# ──────────────────────────────────────────────────────────────────────────────
_session_id: Optional[str] = None
_session_start: Optional[int] = None
_client_label = CLIENT_LABEL

# Self conversation_id detection. The jsonl stem (e.g. "570fe39e-…")
# uniquely identifies a window. Resolution prefers env override → ppid walk
# → mtime heuristic; _self_cid_via records which path won, for whoami().
_self_cid: Optional[str] = None
_self_cid_at: float = 0.0
_self_cid_via: Optional[str] = None  # 'forced' | 'ppid' | 'mtime' | None
_self_cid_ttl_s: float = SELF_CID_TTL_S


def _ensure_cursor(conn: sqlite3.Connection) -> None:
    """First time a session looks at events, anchor cursor to current max so we
    don't drown new sessions in ancient history."""
    if _session_id is None:
        return
    if conn.execute("SELECT 1 FROM cursors WHERE session_id=?", (_session_id,)).fetchone():
        return
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    conn.execute(
        "INSERT INTO cursors (session_id, last_event_id, updated_at) VALUES (?,?,?)",
        (_session_id, max_id, int(time.time())),
    )


def _emit(conn: sqlite3.Connection, kind: str,
          target: Optional[str] = None, summary: Optional[str] = None) -> None:
    """Append to event log + bump heartbeat. Called by every mutating tool."""
    if _session_id is None:
        return
    now = int(time.time())
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?,?,?,?,?)",
        (_session_id, kind, target, (summary or "")[:200], now),
    )
    conn.execute(
        "INSERT INTO presence (session_id, client, started_at, heartbeat_at, "
        "current_thread, last_action) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "  heartbeat_at=excluded.heartbeat_at, "
        "  current_thread=COALESCE(excluded.current_thread, presence.current_thread), "
        "  last_action=excluded.last_action",
        (_session_id, _client_label, _session_start or now, now, target, kind),
    )


def _heartbeat(conn: sqlite3.Connection) -> None:
    """Touch presence without emitting an event (for read-only tool calls)."""
    if _session_id is None:
        return
    conn.execute(
        "UPDATE presence SET heartbeat_at=? WHERE session_id=?",
        (int(time.time()), _session_id),
    )


def _ensure_session(conn: sqlite3.Connection, client: Optional[str] = None) -> str:
    global _session_id, _session_start
    if _session_id is not None:
        _heartbeat(conn)
        conn.commit()
        return _session_id

    if _session_id is None:
        # pid embedded so two processes can never collide; hex tail keeps id short.
        # NB: claude desktop/code may multiplex several windows into one mcp
        # server process — in that case all of them share this _session_id, and
        # session-id-as-client-identity is a known false signal here.
        _session_id = f"s_{os.getpid()}_{secrets.token_hex(2)}"
        _session_start = int(time.time())
        cli = client or _client_label
        conn.execute(
            "INSERT INTO sessions (id, started_at, client, write_origin) "
            "VALUES (?,?,?,?)",
            (_session_id, _session_start, cli, WRITE_ORIGIN),
        )
        conn.execute(
            "INSERT INTO presence (session_id, client, started_at, heartbeat_at, "
            "last_action) VALUES (?,?,?,?,?)",
            (_session_id, cli, _session_start, _session_start, "session_start"),
        )
        _ensure_cursor(conn)
        conn.commit()
        # Lazy imports avoid circular module deps (ingest imports embeddings
        # which imports nothing here — but we still keep this lazy in case
        # the surface widens later).
        try:
            from . import ingest
            ingest._ingest_all(conn, max_msgs=ingest.INGEST_CAP_PER_CALL)
        except Exception:
            pass  # Never block session start on ingestion failure
        try:
            from . import ingest
            ingest._backfill_dialog_fts_if_empty(conn)
        except Exception:
            pass  # FTS unavailable shouldn't block session start
        try:
            from . import ingest
            ingest._start_background_ingester()
        except Exception:
            pass
        try:
            from . import search_proxy
            search_proxy.start_search_proxy()
        except Exception:
            pass
        try:
            from . import spawn_budget
            spawn_budget.start_budget_daemon()
        except Exception:
            pass
        try:
            from . import memory_guard
            memory_guard.start_memory_guard_daemon()
        except Exception:
            pass
        try:
            from . import skill_watcher
            skill_watcher.start_skill_watcher()
        except Exception:
            pass
        try:
            from . import shadow_review
            shadow_review.start_shadow_daemon()
        except Exception:
            pass
        try:
            from . import curator
            curator.start_curator_daemon()
        except Exception:
            pass
        try:
            from . import extract_daemon
            extract_daemon.start_extract_daemon()
        except Exception:
            pass
        try:
            from . import candidate_reviewer
            candidate_reviewer.start_candidate_reviewer_daemon()
        except Exception:
            pass
        try:
            from . import probe_daemon
            probe_daemon.start_probe_daemon()
        except Exception:
            pass
        try:
            from . import evolve_daemon
            evolve_daemon.start_evolve_daemon()
        except Exception:
            pass
        try:
            from . import thread_janitor
            thread_janitor.start_thread_janitor()
        except Exception:
            pass
    return _session_id


_UUID_RE = re.compile(
    r"--(?:resume|session-id|continue)\s+([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"
)


# Which CLI process is hosting this thread-keeper instance. Set once
# at startup by _detect_active_cli() walking the process tree. Used by
# spawn_config.resolve_agent() as the auto-fallback when no manual
# per-role override is present. Values: 'claude' / 'codex' / 'gemini' /
# 'copilot' / None (no recognised host detected).
_active_cli: Optional[str] = None


# Binary-name aliases per CLI. Match is case-insensitive on the
# basename of the parent process command. Claude / Codex have
# Electron-app variants (capitalised, no extension) in addition to
# the lowercase CLI binary.
_CLI_BINARIES = {
    "claude":  (("claude", "claude-code"),   "Claude Code / Claude Desktop"),
    "codex":   (("codex",),                   "OpenAI Codex CLI / desktop"),
    "gemini":  (("gemini",),                  "Google Gemini CLI"),
    "copilot": (("copilot",),                 "GitHub Copilot CLI"),
}


def _detect_active_cli() -> Optional[str]:
    """Walk up the process tree until we find a known CLI binary
    (claude / codex / gemini / copilot). Returns the short name
    ('claude' etc.), or None if no recognised host.

    Identical strategy to _resolve_self_cid_via_ppid — `ps -p $pid -o
    ppid=,command=` repeatedly until we hit a match or run out of
    ancestors. Bounded to 12 levels. Override for tests via
    THREADKEEPER_ACTIVE_CLI env var.
    """
    override = os.environ.get("THREADKEEPER_ACTIVE_CLI", "").strip().lower()
    if override in _CLI_BINARIES:
        return override
    try:
        pid = os.getpid()
    except OSError:
        return None
    for _ in range(12):
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,command="],
                capture_output=True, text=True, timeout=2,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        line = (r.stdout or "").strip()
        if not line:
            return None
        parts = line.split(None, 1)
        if len(parts) < 2:
            return None
        try:
            ppid = int(parts[0])
        except ValueError:
            return None
        cmd = parts[1]
        # Match the binary name as the first whitespace-separated
        # token's basename — guards against false matches on flag
        # values like "--prompt 'claude ...'". Lower-cased for the
        # Electron-app variants (Claude, Codex) that capitalise the
        # binary name on macOS.
        first = cmd.split(None, 1)[0]
        basename = os.path.basename(first.strip('"\'')).lower()
        for cli_name, (aliases, _) in _CLI_BINARIES.items():
            for alias in aliases:
                if (basename == alias
                        or basename.startswith(alias + ".")):
                    return cli_name
        if ppid <= 1:
            return None
        pid = ppid
    return None


def active_cli() -> Optional[str]:
    """Cached accessor — detects once, returns same answer for the
    process lifetime. Exposed so spawn-dispatch + the startup
    validator can ask 'what CLI is hosting us?'"""
    global _active_cli
    if _active_cli is None:
        _active_cli = _detect_active_cli()
    return _active_cli


def _resolve_self_cid_via_ppid() -> Optional[str]:
    """Walk up the process tree until we find a claude CLI invocation with
    --resume/--session-id <uuid>. That uuid IS this conversation's id, with
    zero flap. Bounded to 12 ancestors. macOS-friendly (uses `ps`)."""
    try:
        pid = os.getpid()
    except OSError:
        return None
    for _ in range(12):
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,command="],
                capture_output=True, text=True, timeout=2,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        line = (r.stdout or "").strip()
        if not line:
            return None
        # First column is ppid, rest is command
        parts = line.split(None, 1)
        if len(parts) < 2:
            return None
        try:
            ppid = int(parts[0])
        except ValueError:
            return None
        cmd = parts[1]
        m = _UUID_RE.search(cmd)
        if m:
            return m.group(1)
        if ppid <= 1:
            return None
        pid = ppid
    return None


def _detect_self_cid() -> Optional[str]:
    """Identify THIS conversation's id (jsonl stem). Resolution order:

    1. env THREADKEEPER_FORCE_CID (set by spawn() for children)
    2. ppid walk for `claude ... --resume/--session-id <uuid>` (per-process,
       no flap; cached for the lifetime of the process)
    3. fallback heuristic: latest-mtime jsonl (cached briefly; flaps when
       siblings are equally active)
    """
    global _self_cid, _self_cid_at, _self_cid_via
    forced = os.environ.get("THREADKEEPER_FORCE_CID")
    if forced:
        _self_cid_via = "forced"
        return forced
    # ppid resolution: cache forever once found (process identity is stable).
    if _self_cid and _self_cid_via == "ppid":
        return _self_cid
    if _self_cid is None or _self_cid_via != "ppid":
        cid = _resolve_self_cid_via_ppid()
        if cid:
            _self_cid = cid
            _self_cid_via = "ppid"
            _self_cid_at = time.time()
            return cid
    # Heuristic fallback with short ttl
    now_t = time.time()
    if _self_cid and _self_cid_via == "mtime" and now_t - _self_cid_at < _self_cid_ttl_s:
        return _self_cid
    if not CLAUDE_PROJECTS_DIR.exists():
        return None
    latest_p = None
    latest_m: float = 0.0
    for p in CLAUDE_PROJECTS_DIR.glob("**/*.jsonl"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m > latest_m:
            latest_m = m
            latest_p = p
    if latest_p is None:
        return None
    _self_cid = latest_p.stem
    _self_cid_via = "mtime"
    _self_cid_at = now_t
    return _self_cid
