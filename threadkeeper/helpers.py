"""Stateless utility helpers used across the package: time formatting,
short-quoting, ID generation, process-aliveness check."""
from __future__ import annotations

import os
import random
import secrets
import sqlite3
import subprocess
import time
from typing import Optional

# ±15% wake-up jitter. Every always-on daemon (memory_guard, skill_watcher)
# starts during `_ensure_session` bootstrap on every MCP instance, so with
# several clients open (Code CLI, Desktop, VS Code, headless `claude -p`) they
# all bootstrap near the same moment and would then tick in near-lockstep —
# a synchronized `ps`/notification subprocess storm that scales with instance
# count (#86). Scaling each sleep by a per-tick random factor de-synchronizes
# concurrent instances without meaningfully changing any daemon's cadence.
_JITTER_FRAC = 0.15


def _jittered(seconds: float) -> float:
    """Scale `seconds` by a uniform factor in [1-_JITTER_FRAC, 1+_JITTER_FRAC]."""
    if seconds <= 0:
        return seconds
    return seconds * (1.0 + random.uniform(-_JITTER_FRAC, _JITTER_FRAC))


def daemon_sleep(interval_s, idle_s: float = 30.0) -> None:
    """Sleep one daemon tick without ever busy-spinning, with wake-up jitter.

    Daemon `_serve_loop`s read their interval from a module global that the
    hot-config reload (issue #2) can rewrite at runtime. If a live interval is
    lowered to 0 ("daemon off"), a bare `time.sleep(0)` would turn the loop
    into a CPU-pegging spin. This helper idles for `idle_s` instead so the
    daemon goes quiet (its `run_*_pass` already short-circuits on interval<=0)
    until the knob is raised again.

    The actual sleep is jittered by ±`_JITTER_FRAC` (see above) so multiple
    MCP server instances on one host don't fire their work in lockstep.
    """
    try:
        interval = float(interval_s)
    except (TypeError, ValueError):
        interval = 0.0
    time.sleep(_jittered(interval if interval > 0 else idle_s))


def fmt_age(seconds: int) -> str:
    """Compact human-readable age. 0..59 → 's', then 'm', 'h', 'd'."""
    if seconds < 60:
        return f"{seconds}s"
    m = seconds // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    if h < 24:
        return f"{h}h"
    d = h // 24
    return f"{d}d"


def q(s: str) -> str:
    """Compact double-quote escape for brief lines."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _gen_short_id(conn: sqlite3.Connection, prefix: str, table: str,
                  id_col: str = "id") -> str:
    """prefix + 3 hex chars (4096 unique). Retries on collision; extends to
    5 hex if collision space exhausted (~1M unique fallback)."""
    for _ in range(64):
        cand = prefix + secrets.token_hex(2)[:3]
        if not conn.execute(
            f"SELECT 1 FROM {table} WHERE {id_col}=?", (cand,)
        ).fetchone():
            return cand
    return prefix + secrets.token_hex(3)[:5]


def gen_thread_id(conn: sqlite3.Connection) -> str:
    return _gen_short_id(conn, "T", "threads")


def gen_probe_id(conn: sqlite3.Connection) -> str:
    return _gen_short_id(conn, "P", "probes")


def gen_concept_id(conn: sqlite3.Connection) -> str:
    return _gen_short_id(conn, "C", "concepts")


def gen_distill_id(conn: sqlite3.Connection) -> str:
    return _gen_short_id(conn, "D", "distill")


def gen_dialectic_id(conn: sqlite3.Connection) -> str:
    return _gen_short_id(conn, "UC", "user_dialectic")


def alive(pid: int) -> bool:
    """True if pid corresponds to a running (non-zombie) process. Reaps
    zombies opportunistically when pid is our own child. pid<=0 sentinel
    (used for visible spawns where we don't track) → False."""
    if pid is None or pid <= 0:
        return False
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # Process exists; distinguish zombie via `ps -o state=`. Zombies show
    # 'Z' on macOS/Linux. If ps fails (rare), assume alive.
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            capture_output=True, text=True, timeout=2,
        )
        state = (r.stdout or "").strip()
        if state.startswith("Z") or state == "":
            return False
    except (subprocess.SubprocessError, OSError):
        pass
    return True


# ── Ingest-order watermark (issue #69) ─────────────────────────────────
# The shadow_review and dialectic_miner loops drive their high-water cursor
# off the dialog_messages implicit `rowid` (ingest order), NOT the transcript
# `created_at`. `created_at` is the message's own jsonl timestamp; ingestion
# is not monotonic in it (a dormant/resumed session, a newly-installed
# adapter, or a post-downtime `_ingest_all` backfill lands rows whose
# `created_at` is BELOW a cursor that fresher sessions already pushed
# forward), so a created_at cursor silently steps over those late arrivals.
# `dialog_messages` is append-only (no DELETE / no VACUUM in the package), so
# its rowid is assigned in strict ingest order — a late row always lands
# ABOVE the cursor and is evaluated exactly once, and the monotonic advance
# means shadow_review never re-spawns a window it already saw.
#
# Pre-#69 deployments stored a created_at unix timestamp in `events.target`.
# A rowid is orders of magnitude smaller than any real unix timestamp, so a
# stored watermark at or above this floor is a legacy created_at value we
# translate to the matching rowid once (the next pass overwrites it with a
# real rowid). 1_000_000_000 is 2001-09-09; every real created_at exceeds it.
LEGACY_TS_FLOOR = 1_000_000_000


def dialog_rowid_at_or_before(conn: sqlite3.Connection, created_at_ts: int) -> int:
    """Largest dialog_messages rowid whose created_at <= `created_at_ts`.

    Used to (a) translate a legacy created_at watermark into an ingest-order
    rowid on the first read after the #69 upgrade and (b) seed the first-ever
    shadow window so it doesn't replay the whole transcript history. Returns 0
    when no row is that old (or on a missing table)."""
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM dialog_messages "
            "WHERE created_at <= ?",
            (int(created_at_ts),),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0)


def resolve_ingest_watermark(conn: sqlite3.Connection, stored: int) -> int:
    """Interpret a stored cursor value as a dialog_messages ingest-order rowid.

    A value >= LEGACY_TS_FLOOR is a pre-#69 created_at timestamp → translate
    to the equivalent rowid. Smaller positives are already rowids. 0/negative
    → 0 (no cursor yet)."""
    if stored <= 0:
        return 0
    if stored >= LEGACY_TS_FLOOR:
        return dialog_rowid_at_or_before(conn, stored)
    return stored


def normalize_text(s: str) -> str:
    """Whitespace-collapsed lower for fuzzy duplicate detection."""
    return " ".join(s.lower().strip().split())


def _fts_query(raw: str) -> str:
    """Turn a raw user query into a safe FTS5 MATCH string.

    FTS5 parses '-', '"', '*', '(', ')', ':', 'OR' etc. as query syntax, so
    an everyday query like 'zebra-quux' or 'what about X?' raises an
    OperationalError (surfaced as 'fts_error' from search(), or a silent
    empty result from the brief()/dialog_search FTS fallbacks). We quote
    each whitespace-separated term as a phrase: operators inside become
    literal, while the tokenizer still splits the phrase on punctuation so
    matching is unchanged. Embedded double-quotes are doubled (FTS5's own
    escape). Pure-punctuation tokens (no alphanumeric content) are dropped.

    Returns '' when the query has no tokenizable content — callers should
    treat that as 'no query' and skip MATCH rather than run it."""
    terms: list[str] = []
    for tok in raw.split():
        if any(ch.isalnum() for ch in tok):
            terms.append('"' + tok.replace('"', '""') + '"')
    return " ".join(terms)
