"""Cross-process daemon cadence gate (persisted last-run per loop).

Every MCP server process runs its own copy of each interval daemon, and each
copy keeps its schedule in process memory. A freshly started server therefore
considers every daemon "overdue" and fires it immediately — with several CLI
sessions open, an "every 3 days" curator can run dozens of times a day (one
pass per new server start), and the single-flight lock only stops *concurrent*
passes, not frequent sequential ones.

This module persists per-daemon last-run timestamps in the shared DB so the
cadence survives process churn. The claim is one atomic upsert: whichever
process claims first wins the slot; everyone else sees "not due" until the
interval elapses. Scheduled loop ticks claim with `scheduled=True`; manual /
tool-invoked passes bypass the gate but still record the run so the next
scheduled fire lands a full interval later.
"""
from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

# A scheduled tick is due when the stored last run is at least this fraction
# of the interval old. `daemon_sleep()` jitters ticks by ±15%, so requiring
# the full interval would reject every early wake-up and drift the effective
# cadence well past the knob; 0.8 absorbs the jitter while still collapsing
# the every-new-server refire into one pass per interval.
_DUE_FRACTION = 0.8


def claim_pass(
    name: str,
    interval_s: float,
    *,
    scheduled: bool = False,
    conn: sqlite3.Connection | None = None,
    now: float | None = None,
) -> bool:
    """Record a pass start; for scheduled ticks, only when the interval elapsed.

    Returns True when the caller should run the pass. The scheduled path is a
    single atomic upsert (WAL serializes writers), so two servers waking at the
    same moment cannot both win the slot. Fail-open: any storage error grants
    the claim — worst case is the pre-gate behavior.
    """
    ts = int(now if now is not None else time.time())
    try:
        if conn is None:
            from .db import get_db

            conn = get_db()
        if not scheduled:
            conn.execute(
                "INSERT INTO daemon_state(name, last_run_at) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "last_run_at=excluded.last_run_at",
                (name, ts),
            )
            conn.commit()
            return True
        try:
            gap = max(0.0, float(interval_s)) * _DUE_FRACTION
        except (TypeError, ValueError):
            gap = 0.0
        cur = conn.execute(
            "INSERT INTO daemon_state(name, last_run_at) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "last_run_at=excluded.last_run_at "
            "WHERE excluded.last_run_at - daemon_state.last_run_at >= ?",
            (name, ts, gap),
        )
        conn.commit()
        return bool(cur.rowcount)
    except sqlite3.Error:
        logger.debug("daemon_state claim failed for %r", name, exc_info=True)
        return True


def last_run_at(
    name: str, conn: sqlite3.Connection | None = None
) -> int | None:
    """Stored last-run timestamp for a loop, or None when it never ran."""
    try:
        if conn is None:
            from .db import get_db

            conn = get_db()
        row = conn.execute(
            "SELECT last_run_at FROM daemon_state WHERE name=?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return int(row[0])
