"""Extract daemon — periodic auto-harvest of decision-shaped utterances
from dialog_messages into the extract_candidates queue.

Architecture mirror of shadow_review:

  1. Daemon thread wakes every EXTRACT_INTERVAL_S seconds (0 = off).
  2. Runs the same heuristics as extract_recent(), but scans by the
     ingest-order rowid cursor (issue #69, same scheme as shadow_review and
     dialectic_miner): nothing falls between ticks, a capped batch drains on
     the next pass, and a late/out-of-order ingested message (old created_at,
     fresh rowid) is harvested exactly once instead of falling below a
     wall-clock cutoff.
  3. Records events.kind='extract_pass' with the per-pass counters so
     `extract_review_status()` can show health at a glance; `target` carries
     the rowid high-water mark (legacy created_at watermarks are translated
     once on first read).

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
from .helpers import (
    daemon_sleep,
    dialog_rowid_at_or_before,
    resolve_ingest_watermark,
)
from . import daemon_state, identity

logger = logging.getLogger(__name__)

_started = False


def _last_extract_rowid(conn: sqlite3.Connection) -> int:
    """Ingest-order rowid high-water mark for the extract daemon (issue #69).

    The watermark in the latest `events.kind='extract_pass'.target` is a
    dialog_messages rowid (ingest order), not a transcript timestamp, so a
    late/out-of-order ingested message can't fall below it — a created_at
    cursor silently stepped over post-downtime backfills and freshly-installed
    adapters. A pre-migration watermark held a created_at timestamp; it is
    translated to the matching rowid once. Returns 0 when no prior pass."""
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
        stored = int(row["target"])
    except (ValueError, TypeError):
        return 0
    return resolve_ingest_watermark(conn, stored)


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


def run_extract_pass(force: bool = False, *, scheduled: bool = False) -> str:
    """Execute one extract pass synchronously. Used by the daemon AND by
    the MCP tool for manual triggering.

    Returns the same status string `extract_recent` returns ("ok
    window=… scanned=… verbatim=… distill=… concept=… note=…
    skipped_existing=…" or "no_dialog window=…m"), or 'not_due' for a
    scheduled tick when another server already ran this loop within the
    interval. Plus advances the `extract_pass` rowid cursor.
    """
    if EXTRACT_INTERVAL_S <= 0 and not force:
        return "disabled"
    if not daemon_state.claim_pass(
        "extract", EXTRACT_INTERVAL_S, scheduled=scheduled,
    ):
        return "not_due"
    conn = get_db()
    started_at = int(time.time())
    floor = _last_extract_rowid(conn)
    if floor <= 0:
        # First-ever pass: seed the floor from the lookback window so a
        # long-running install doesn't replay its whole transcript history.
        floor = dialog_rowid_at_or_before(
            conn, started_at - max(1, int(EXTRACT_WINDOW_MIN)) * 60,
        )
    # Late import — tools.extract registers MCP tools at import time, and
    # the daemon module loads before all tools are registered.
    from .tools.extract import _extract_from_rowid
    try:
        result, high_water = _extract_from_rowid(
            floor, window_min=EXTRACT_WINDOW_MIN,
        )
    except Exception as e:
        logger.debug("extract_daemon: pass failed", exc_info=True)
        _record_extract_pass(conn, floor, f"error: {e}")
        return f"error: {e}"
    _record_extract_pass(conn, high_water, str(result)[:200])
    return str(result)


def _serve_loop() -> None:
    """Daemon body. Sleep → tick → sleep, until process dies."""
    while True:
        try:
            run_extract_pass(scheduled=True)
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
