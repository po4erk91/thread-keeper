"""Detection and cleanup of orphaned thread-keeper server processes.

Each Claude client (Code CLI, Desktop, VS Code extension, headless `claude -p`)
spawns its own thread-keeper subprocess via stdio MCP. When the client dies
cleanly, its subprocess gets reaped. When the client crashes / is killed -9 /
loses its parent, the thread-keeper can linger as an orphan: still holding
file handles, embedding model in RAM, but with no peer ever sending it stdin.

Detection criteria (a process is "orphaned" when ALL hold):
  1. Process is a threadkeeper.server invocation
  2. Parent process is gone (ppid is 1/launchd OR ppid doesn't exist)
  3. Either:
     - heartbeat_at on its session row is older than `STALE_HEARTBEAT_S`, OR
     - the process has no session row in `presence` (it never finished
       bootstrapping)

Cleanup never touches the running parent process itself — only other
thread-keeper processes that meet the orphan criteria.

Public API:
  scan() -> list[dict]      # diagnostic snapshot of all mp processes
  cleanup(dry_run, force) -> dict   # kill orphans
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

from .db import get_db
from .helpers import alive


# Seconds of presence-table silence before we consider a process orphaned.
STALE_HEARTBEAT_S = 5 * 60
SERVER_COMMAND_MARKER = "threadkeeper.server"


# ─────────────────────────────────────────────────────────────────────
# Process discovery
# ─────────────────────────────────────────────────────────────────────

def _list_threadkeeper_pids() -> list[dict]:
    """Find every running threadkeeper.server invocation. Returns rows
    with pid, ppid, rss_kb, etime_s, full command. Skips disclaimer
    wrappers (parent shim that exec's the real Python and exits)."""
    try:
        r = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid=,rss=,etime=,command="],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    out: list[dict] = []
    for line in (r.stdout or "").splitlines():
        if not _is_threadkeeper_server_command(line):
            continue
        # Tokenize: pid ppid rss etime command...
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            rss = int(parts[2])
        except ValueError:
            continue
        etime = parts[3]
        cmd = parts[4]
        out.append({
            "pid": pid,
            "ppid": ppid,
            "rss_kb": rss,
            "etime": etime,
            "command": cmd,
        })
    return out


def _pid_alive(pid: int) -> bool:
    """True if the given pid exists. pid=1 (init/launchd) and pid<=0 return
    False — we treat init as 'no real parent'. Zombies return False."""
    if pid is None or pid <= 1:
        return False
    return alive(pid)


def _is_threadkeeper_server_command(command: str | None) -> bool:
    """True when a ps command line belongs to the real server process."""
    if not command:
        return False
    if SERVER_COMMAND_MARKER not in command:
        return False
    # Skip the disclaimer shim: its command starts with the
    # /Applications/Claude.app/Contents/Helpers/disclaimer path and holds
    # RSS ≈0. We want only the real Python that took its place.
    return "/Helpers/disclaimer" not in command


def _current_command_for_pid(pid: int) -> str | None:
    """Return the current command line for pid, or None if unavailable."""
    if pid is None or pid <= 0:
        return None
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    cmd = (r.stdout or "").strip()
    return cmd or None


def is_threadkeeper_server_pid(pid: int) -> bool:
    """Re-read pid identity and confirm it is still a thread-keeper server."""
    return _is_threadkeeper_server_command(_current_command_for_pid(pid))


def signal_if_threadkeeper(pid: int, sig: int) -> tuple[bool, str | None]:
    """Signal pid only after verifying it still belongs to thread-keeper.

    Returns (sent, skip_reason). os.kill errors still propagate so callers can
    report real races that happen after the identity check.
    """
    if not is_threadkeeper_server_pid(pid):
        return False, "pid_no_longer_threadkeeper_server"
    os.kill(pid, sig)
    return True, None


# ─────────────────────────────────────────────────────────────────────
# Orphan classification
# ─────────────────────────────────────────────────────────────────────

def _heartbeat_age_for_pid(conn, pid: int) -> Optional[int]:
    """Look up presence.heartbeat_at for the session that this pid most
    likely belongs to. Heuristic: pid embedded in session_id format
    `s_<pid>_<hex>`. Returns age in seconds, or None if no match."""
    row = conn.execute(
        "SELECT heartbeat_at FROM presence "
        "WHERE session_id LIKE ? "
        "ORDER BY heartbeat_at DESC LIMIT 1",
        (f"s_{pid}_%",),
    ).fetchone()
    if not row or not row["heartbeat_at"]:
        return None
    return int(time.time()) - int(row["heartbeat_at"])


def classify(p: dict, conn) -> dict:
    """Return p augmented with orphan classification. Sets:
      - `parent_alive` (bool)
      - `heartbeat_age_s` (int | None)
      - `is_orphaned` (bool)
      - `is_self` (bool) — never classify our own pid as orphan
    """
    p = dict(p)
    p["parent_alive"] = _pid_alive(p["ppid"])
    p["heartbeat_age_s"] = _heartbeat_age_for_pid(conn, p["pid"])
    p["is_self"] = (p["pid"] == os.getpid())

    if p["is_self"]:
        p["is_orphaned"] = False
        p["orphan_reason"] = "self"
        return p

    if p["parent_alive"]:
        p["is_orphaned"] = False
        p["orphan_reason"] = "parent_alive"
        return p

    # Parent gone. Now check heartbeat freshness.
    hb = p["heartbeat_age_s"]
    if hb is None:
        # No presence row — process either died before bootstrapping or
        # uses a different session-id format. Treat as orphan to be safe;
        # if it's a real living process it'll come back next session.
        p["is_orphaned"] = True
        p["orphan_reason"] = "parent_gone + no_heartbeat"
        return p
    if hb > STALE_HEARTBEAT_S:
        p["is_orphaned"] = True
        p["orphan_reason"] = f"parent_gone + heartbeat_age={hb}s > {STALE_HEARTBEAT_S}s"
        return p
    p["is_orphaned"] = False
    p["orphan_reason"] = f"parent_gone but heartbeat fresh ({hb}s)"
    return p


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

def scan() -> list[dict]:
    """Return a list of classified thread-keeper processes."""
    conn = get_db()
    procs = _list_threadkeeper_pids()
    return [classify(p, conn) for p in procs]


def cleanup(dry_run: bool = True, force: bool = False) -> dict:
    """Kill orphaned processes. dry_run=True returns the plan without
    killing. force=True sends SIGKILL instead of SIGTERM (which gives the
    process a chance to flush)."""
    import signal as _sig
    procs = scan()
    plan = [p for p in procs if p.get("is_orphaned")]
    killed: list[int] = []
    failed: list[dict] = []
    skipped: list[dict] = []
    if not dry_run:
        sig = _sig.SIGKILL if force else _sig.SIGTERM
        for p in plan:
            try:
                sent, reason = signal_if_threadkeeper(p["pid"], sig)
                if sent:
                    killed.append(p["pid"])
                else:
                    skipped.append({"pid": p["pid"], "reason": reason})
            except (ProcessLookupError, PermissionError) as e:
                failed.append({"pid": p["pid"], "err": str(e)})
            except OSError as e:
                failed.append({"pid": p["pid"], "err": str(e)})
    return {
        "all_procs": procs,
        "orphans": plan,
        "killed": killed,
        "failed": failed,
        "skipped": skipped,
        "dry_run": dry_run,
    }
