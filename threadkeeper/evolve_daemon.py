"""Evolve reviewer daemon — autonomous triage of the format-evolution queue.

`evolve_format()` lets any session file a suggestion to improve thread-keeper's
own format/protocol. The audit found the predictable failure: 5 filed, 0 ever
actioned — a write-only graveyard, because nothing reviewed it and applying a
suggestion is friction nobody picks up mid-task.

This daemon keeps the queue HONEST (it does not — must not — apply suggestions;
applying edits format/code, a foreground/human action). Each pass spawns a
context-free child that reviews the pending suggestions and, per item, calls
evolve_decide():
  - PROMOTE — still relevant + worth doing → brief surfaces it sharply (★) so
    the foreground agent / human actually applies it.
  - DISMISS — duplicate, superseded, or stale → drops out of the queue.

Mirror of probe_daemon / curator: weekly cadence, foreground-only,
machine-wide single-flight via the prompt prefix, advisory child with a
narrow tool surface (evolve_review + evolve_decide, no code/format mutation).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from .config import EVOLVE_REVIEW_INTERVAL_S, EVOLVE_REVIEW_MIN
from .db import get_db
from . import identity

logger = logging.getLogger(__name__)

_started = False

# First line of the prompt injected into the reviewer child. Added to
# shadow_review._INTERNAL_PROMPT_PREFIXES so the child's transcript doesn't
# pollute extract/shadow windows when ingested back.
EVOLVE_PROMPT_PREFIX = "You are an EVOLVE REVIEWER"

EVOLVE_PROMPT = """\
You are an EVOLVE REVIEWER for thread-keeper. Below is the queue of PENDING
format-evolution suggestions — proposals to improve thread-keeper's own
brief format, note kinds, nudges, or tool surface, filed by past sessions.

Your job is TRIAGE, not implementation. You do NOT apply any suggestion
(applying edits format/code — that's a foreground/human action). For EACH
pending suggestion call exactly one:

  evolve_decide(evolve_id=<id>, decision='promote', reason='<why>')
     — the suggestion is still relevant and worth doing. Promoting surfaces
       it sharply in the brief (★) so the foreground agent / human applies it.

  evolve_decide(evolve_id=<id>, decision='dismiss', reason='<why>')
     — the suggestion is a DUPLICATE of another (say which id), already
       superseded, or STALE (references a brief field / behavior that no
       longer exists). Be specific in the reason.

Guidance:
- When two suggestions overlap, PROMOTE the clearest one and DISMISS the
  rest as "duplicate of #<id>".
- Prefer DISMISS for vague or one-off ergonomic gripes; PROMOTE only
  concrete, durable improvements.
- If genuinely unsure, leave it (don't call evolve_decide for that id).

When done, output the single line EVOLVE_REVIEW_COMPLETE. Do NOT cite
internal IDs other than the evolve #ids in the queue. No other tools.

PENDING SUGGESTIONS
===================
"""


def _last_evolve_ts(conn: sqlite3.Connection) -> int:
    """High-water timestamp of the most recent evolve-review pass, or 0."""
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='evolve_review_pass' "
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


def _record_evolve_pass(conn: sqlite3.Connection, ts: int,
                        outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, "
            "created_at) VALUES (?, 'evolve_review_pass', ?, ?, ?)",
            (identity._session_id or "", str(ts), outcome[:300],
             int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("evolve_daemon: failed to record pass", exc_info=True)


def _pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Pending suggestions: not applied, not yet triaged."""
    try:
        return conn.execute(
            "SELECT id, suggestion, rationale FROM evolve "
            "WHERE applied=0 AND COALESCE(status,'pending')='pending' "
            "ORDER BY created_at ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _running_evolve_children(conn: sqlite3.Connection) -> list[str]:
    """Running reviewer task ids, reaping dead rows. Machine-wide
    single-flight: one evolve reviewer at a time across all servers."""
    from .helpers import alive
    try:
        rows = conn.execute(
            "SELECT id, pid FROM tasks WHERE ended_at IS NULL "
            "AND prompt LIKE ?",
            (EVOLVE_PROMPT_PREFIX + "%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    now = int(time.time())
    running: list[str] = []
    touched = False
    for r in rows:
        pid = int(r["pid"] or 0)
        if pid > 0 and not alive(pid):
            conn.execute(
                "UPDATE tasks SET ended_at=? WHERE id=? AND ended_at IS NULL",
                (now, r["id"]),
            )
            touched = True
            continue
        running.append(r["id"])
    if touched:
        conn.commit()
    return running


def run_evolve_pass(force: bool = False) -> str:
    """One evolve-review pass.

    Status strings:
      'disabled'                  — knob off and not forced
      'no_pending'                — nothing to triage
      'below_min n=<k>'           — fewer than EVOLVE_REVIEW_MIN pending
      'reviewer_running n=<k>'    — a reviewer child is already in flight
      'spawned n=<k> …'           — launched the reviewer child
      'spawn_error: …'            — spawn rejected
    """
    if EVOLVE_REVIEW_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    now_t = int(time.time())
    pending = _pending(conn)
    if not pending:
        _record_evolve_pass(conn, now_t, "no_pending")
        return "no_pending"
    if len(pending) < EVOLVE_REVIEW_MIN:
        out = f"below_min n={len(pending)}"
        _record_evolve_pass(conn, now_t, out)
        return out

    running = _running_evolve_children(conn)
    if running:
        out = f"reviewer_running n={len(running)}"
        _record_evolve_pass(conn, now_t, out)
        return out

    queue = "\n".join(
        f"#{r['id']}: {r['suggestion']}"
        + (f"\n    rationale: {r['rationale']}" if r["rationale"] else "")
        for r in pending
    )
    prompt = EVOLVE_PROMPT + queue

    from .tools.spawn import spawn  # late import — avoids import cycle
    try:
        result = spawn(
            prompt=prompt,
            visible=False,
            capture_output=True,
            permission_mode="auto",
            role="evolve_reviewer",
            write_origin="evolve",
            slim=True,
            extra_allowed_tools=(
                "mcp__thread-keeper__evolve_review,"
                "mcp__thread-keeper__evolve_decide"
            ),
        )
    except Exception as e:  # noqa: BLE001 — never crash the daemon
        out = f"spawn_error: {e}"
        _record_evolve_pass(conn, now_t, out)
        return out
    out = f"spawned n={len(pending)} {str(result)[:120]}"
    _record_evolve_pass(conn, now_t, out)
    return out


def _serve_loop() -> None:
    while True:
        try:
            run_evolve_pass()
        except Exception:
            logger.debug("evolve_daemon tick failed", exc_info=True)
        time.sleep(EVOLVE_REVIEW_INTERVAL_S)


def start_evolve_daemon() -> None:
    """Idempotent starter. No-op when EVOLVE_REVIEW_INTERVAL_S<=0. Same
    cascade prevention as the other daemons: spawned children / non-
    foreground origins refuse to start it so spawn() can't recurse."""
    global _started
    if _started:
        return
    if EVOLVE_REVIEW_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(
        target=_serve_loop, name="evolve_daemon", daemon=True,
    )
    t.start()
    _started = True
