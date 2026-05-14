"""Stateless utility helpers used across the package: time formatting,
short-quoting, ID generation, process-aliveness check."""
from __future__ import annotations

import os
import secrets
import sqlite3
import subprocess
from typing import Optional


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


def normalize_text(s: str) -> str:
    """Whitespace-collapsed lower for fuzzy duplicate detection."""
    return " ".join(s.lower().strip().split())
