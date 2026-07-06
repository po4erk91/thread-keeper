"""Thread-janitor daemon — autonomously close stale threads so abandoned
work gets harvested into skills.

The skill-harvest path is event-driven: `close_thread()` fires the
auto-review hook, which spawns a background child that materializes a skill
from a rich closed thread. But that path only runs when threads actually
CLOSE — and in practice they don't: the user never closes threads, and the
agent rarely remembers to. The audit found 32 threads open (some idle 12d),
2 auto-review spawns ever, 5 skills from 115 closes. The harvest machinery
was starved of its trigger.

This daemon supplies the trigger. Each pass it finds threads idle past
THREAD_IDLE_CLOSE_DAYS and closes them via the normal `close_thread()` path,
so the existing auto-review hook fires (for the richest pending thread) and
the brief's skill_hint surfaces the rest for the foreground agent.

Aggressive auto-close is safe ONLY because closing is reversible: a note()
on a closed thread revives it to active (see tools/threads.note). Returning
to a topic — i.e. adding a note — reopens it. So the janitor can close
freely; nothing is lost, just parked.

Mirror of the other daemons: interval knob (0 = off), foreground-only via
BACKGROUND_DAEMONS_ALLOWED so spawned children don't recurse, idempotent
(already-closed threads don't re-match), records a `janitor_pass` event for
observability / the dashboard.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from .config import THREAD_JANITOR_INTERVAL_S, THREAD_IDLE_CLOSE_DAYS
from .db import get_db
from .helpers import daemon_sleep
from . import daemon_state, identity

logger = logging.getLogger(__name__)

_started = False


def _record_janitor_pass(conn: sqlite3.Connection, outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, "
            "created_at) VALUES (?, 'janitor_pass', '', ?, ?)",
            (identity._session_id or "", outcome[:300], int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("thread_janitor: failed to record pass", exc_info=True)


def _last_janitor_outcome(conn: sqlite3.Connection) -> str | None:
    """Summary of the most recent recorded janitor_pass, or None."""
    try:
        row = conn.execute(
            "SELECT summary FROM events WHERE kind='janitor_pass' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["summary"] if row else None


def _stale_threads(conn: sqlite3.Connection, cutoff: int) -> list[sqlite3.Row]:
    """Active or idle threads not touched since `cutoff`, oldest first."""
    try:
        return conn.execute(
            "SELECT id, question FROM threads "
            "WHERE state IN ('active','idle') AND last_touched_at < ? "
            "ORDER BY last_touched_at ASC",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def run_janitor_pass(force: bool = False, *, scheduled: bool = False) -> str:
    """One janitor pass: close every thread idle past the threshold via
    close_thread() (which fires the auto-review hook). Returns a short
    status string for observability:

      'disabled'        — knob off and not forced
      'not_due'         — scheduled tick, another server already ran this
                          loop within the interval (daemon_state)
      'no_stale'        — nothing past the idle threshold
      'closed=N'        — closed N stale threads
    """
    if THREAD_JANITOR_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    if not daemon_state.claim_pass(
        "thread_janitor", THREAD_JANITOR_INTERVAL_S,
        scheduled=scheduled, conn=conn,
    ):
        return "not_due"
    now = int(time.time())
    cutoff = now - int(max(0.0, THREAD_IDLE_CLOSE_DAYS) * 86400)
    stale = _stale_threads(conn, cutoff)
    if not stale:
        # Collapse consecutive no-op ticks: record `no_stale` only on the
        # transition into quiet, not on every tick. Otherwise the `events`
        # table grows one zero-signal row per interval forever — rows that
        # brief()/nudge queries then have to scan (#86). The first no_stale
        # after any activity still lands, so the dashboard keeps a heartbeat.
        if _last_janitor_outcome(conn) != "no_stale":
            _record_janitor_pass(conn, "no_stale")
        return "no_stale"

    # Late import — tools.threads imports brief/embeddings; importing at
    # module load would risk a cycle. close_thread() owns the state change,
    # the close event, AND the auto-review hook, so routing through it keeps
    # the janitor's closes indistinguishable from a manual close.
    from .tools.threads import close_thread

    days = THREAD_IDLE_CLOSE_DAYS
    days_disp = int(days) if float(days).is_integer() else days
    outcome = f"auto-closed by janitor: idle > {days_disp}d (reopen via note)"
    closed = 0
    for t in stale:
        try:
            res = close_thread(thread_id=t["id"], outcome=outcome)
            if isinstance(res, str) and res.startswith("ok"):
                closed += 1
        except Exception:  # noqa: BLE001 — never crash the daemon on one row
            logger.debug("thread_janitor: close failed for %s",
                         t["id"], exc_info=True)
    out = f"closed={closed}"
    _record_janitor_pass(conn, out)
    return out


def _serve_loop() -> None:
    while True:
        try:
            run_janitor_pass(scheduled=True)
        except Exception:
            logger.debug("thread_janitor tick failed", exc_info=True)
        daemon_sleep(THREAD_JANITOR_INTERVAL_S)


def start_thread_janitor() -> None:
    """Idempotent starter. No-op when THREAD_JANITOR_INTERVAL_S<=0. Same
    cascade prevention as the other daemons: spawned children / non-
    foreground origins refuse to start it, so a review child the janitor
    triggers can't spin up its own janitor."""
    global _started
    if _started:
        return
    if THREAD_JANITOR_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(
        target=_serve_loop, name="thread_janitor", daemon=True,
    )
    t.start()
    _started = True
