"""Extract daemon — periodic auto-harvest of decision-shaped utterances
from dialog_messages into the extract_candidates queue.

Architecture mirror of shadow_review:

  1. Daemon thread wakes every EXTRACT_INTERVAL_S seconds (0 = off).
  2. Calls extract_recent(window_min=EXTRACT_WINDOW_MIN) — same logic as
     the MCP tool, just invoked automatically instead of waiting for the
     agent to remember to call it.
  3. Records events.kind='extract_pass' with the per-pass counters so
     `extract_review_status()` can show health at a glance.

Where shadow_review extracts CLASS-LEVEL durable RULES (skills, lessons),
extract harvests PER-INCIDENT DECISION-SHAPED utterances and adds them
to the agent's review queue. The agent then materializes the survivors
via review_candidates() + accept_candidate().

Designed around the empirical finding (audit log, 2026-05-16): no
parallel session calls `note()` / `verbatim_user()` / `open_thread()`
on its own. Memory bookkeeping fights against the agent's primary task
focus. This daemon side-steps the incentive problem by harvesting from
what the agent ALREADY said out loud — no agent-side action required.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from .config import EXTRACT_INTERVAL_S, EXTRACT_WINDOW_MIN
from .db import get_db
from .helpers import daemon_sleep
from . import identity

logger = logging.getLogger(__name__)

_started = False


def _last_extract_ts(conn: sqlite3.Connection) -> int:
    """High-water timestamp of the most recent extract pass, or 0."""
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='extract_pass' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row or not row["target"]:
        return 0
    try:
        return int(row["target"])
    except (ValueError, TypeError):
        return 0


def _record_extract_pass(conn: sqlite3.Connection,
                         ts: int,
                         outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, "
            "created_at) VALUES (?, 'extract_pass', ?, ?, ?)",
            (identity._session_id or "", str(ts), outcome[:300],
             int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("extract_daemon: failed to record pass", exc_info=True)


def run_extract_pass(force: bool = False) -> str:
    """Execute one extract pass synchronously. Used by the daemon AND by
    the MCP tool for manual triggering.

    Returns the same status string `extract_recent` returns ("ok
    window=… scanned=… verbatim=… distill=… concept=… note=…
    skipped_existing=…" or "no_dialog window=…m"). Plus advances the
    `extract_pass` cursor for telemetry.
    """
    if EXTRACT_INTERVAL_S <= 0 and not force:
        return "disabled"
    # Late import — tools.extract registers MCP tools at import time, and
    # the daemon module loads before all tools are registered.
    from .tools.extract import extract_recent
    try:
        result = extract_recent(window_min=EXTRACT_WINDOW_MIN)
    except Exception as e:
        logger.debug("extract_daemon: pass failed", exc_info=True)
        _record_extract_pass(get_db(), int(time.time()),
                             f"error: {e}")
        return f"error: {e}"
    _record_extract_pass(get_db(), int(time.time()), str(result)[:200])
    return str(result)


def _serve_loop() -> None:
    """Daemon body. Sleep → tick → sleep, until process dies."""
    while True:
        try:
            run_extract_pass()
        except Exception:
            logger.debug("extract_daemon tick failed", exc_info=True)
        daemon_sleep(EXTRACT_INTERVAL_S)


def start_extract_daemon() -> None:
    """Idempotent daemon starter. Honors env: no-op when
    EXTRACT_INTERVAL_S<=0. Same cascade-prevention as shadow_review:
    spawned/background children refuse to start the daemon so spawn()
    doesn't recurse."""
    global _started
    if _started:
        return
    if EXTRACT_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED, SEMANTIC_AVAILABLE
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    if not SEMANTIC_AVAILABLE:
        return  # slim child: don't fire extract from here
    t = threading.Thread(
        target=_serve_loop, name="extract_daemon", daemon=True,
    )
    t.start()
    _started = True
