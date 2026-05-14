"""Shadow-review daemon: an autonomous observer that periodically scans
recently-ingested dialog and decides whether any class-level learning
emerged worth materializing into a Claude skill — independent of whether
foreground Claude bothered to call close_thread.

The architecture has two layers:

1. PURE FUNCTIONS (below) — read dialog_messages diff since last pass,
   build a context dump, decide whether the window is worth evaluating
   at all (cheap char-count floor). Idempotent: tracks last-evaluated
   timestamp via events.kind='shadow_review_pass'.

2. DAEMON / SPAWN (start_shadow_daemon) — periodic thread in the parent
   thread-keeper process. On each tick: collect candidate window, if
   non-trivial → spawn slim child with SHADOW_REVIEW_PROMPT + dialog
   dump. Child IS the LLM evaluator; it decides yes/no and (when yes)
   applies skill_manage / mark_skill_materialized.

Why this exists: foreground Claude is an unreliable narrator of when to
close threads / materialize skills. The shadow pass closes that gap.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Optional

from .config import (
    SHADOW_REVIEW_INTERVAL_S,
    SHADOW_REVIEW_MIN_CHARS,
    SHADOW_REVIEW_WINDOW_S,
)
from .db import get_db
from . import identity

logger = logging.getLogger(__name__)

_started = False


# The shadow prompt is what the spawned evaluator child sees. It encodes
# the class-vs-incident decision rubric inline so the child doesn't need
# to (and can't, in slim mode) load the ai-memory-learning-loop skill.
from .i18n import SHADOW_CLASS_SIGNAL_EXAMPLES

SHADOW_REVIEW_PROMPT = f"""\
You are a SHADOW LEARNING OBSERVER for thread-keeper. You read a slice
of recent dialog from across ALL Claude sessions on this machine and
decide whether any CLASS-LEVEL learning emerged that's worth a durable
skill under ~/.claude/skills/.

CLASS-LEVEL signals (materialize):
{SHADOW_CLASS_SIGNAL_EXAMPLES}\
- a debugging insight that generalizes beyond the specific bug
- a workflow rule the user stated as policy
- a corrected misunderstanding (existing skill is wrong/incomplete)

NOT class-level (skip):
- one-off task descriptions
- environment-specific fixes ("install pip", "wrong dir")
- session-transient confusion
- the user asking what something is
- you complimenting yourself or summarizing what just happened

PROCEDURE
1. Read the dialog window below.
2. If nothing class-level emerges → output exactly `SKIP: <one-line reason>` and stop.
3. If class-level learning is present:
   a. Decide PATCH existing skill vs CREATE new umbrella vs ADD reference file.
   b. Call `mcp__thread-keeper__skill_manage(action=..., name=..., ...)`.
      - Naming: lowercase-hyphens, describes a CLASS of task, not the incident.
      - PATCH preferred when a relevant skill already exists.
   c. Output `MATERIALIZED: <skill_name> (<action>)` on success.

CONSTRAINTS
- Be conservative. False negatives (skipping) cost nothing; false
  positives pollute the skill store.
- Do NOT open/close memory threads. Your sole output is a skill write
  or SKIP.
- Do NOT cite internal IDs in human-readable output (no T-codes, cids,
  task IDs). The user style requires plain prose.

DIALOG WINDOW (most recent at the bottom)
=========================================
"""


def _last_shadow_ts(conn: sqlite3.Connection) -> int:
    """Earliest dialog-message timestamp we have NOT yet evaluated.

    Returns the high-water mark recorded in the most recent
    `events.kind='shadow_review_pass'` row. The mark lives in `target`
    so `summary` is free for the human-readable outcome.
    Returns 0 when no prior pass exists — caller falls back to a
    window-based floor.
    """
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='shadow_review_pass' "
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


def _record_shadow_pass(conn: sqlite3.Connection,
                        high_water_ts: int,
                        outcome: str) -> None:
    """Write a shadow_review_pass event so the next tick advances cursor.

    `high_water_ts` is the created_at of the newest dialog message we
    evaluated (stored in `target` for cursor reads). `outcome` is a
    short human-readable status string stored in `summary` (e.g.
    'no_window', 'spawned task_id=...', 'too_short').
    """
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'shadow_review_pass', ?, ?, ?)",
            (identity._session_id or "", str(high_water_ts),
             outcome[:300], int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("shadow: failed to record pass", exc_info=True)


def _collect_window(conn: sqlite3.Connection,
                    floor_ts: int,
                    window_s: int) -> tuple[str, int, int]:
    """Pull dialog messages newer than max(floor_ts, now-window_s).

    Returns (dump_text, high_water_ts, char_count).
      - dump_text: human-readable rendering ready for the shadow prompt
      - high_water_ts: largest created_at seen (== floor for next tick)
      - char_count: total visible char length (input to MIN_CHARS guard)

    Mixes all active sessions — that's the point. The shadow agent's
    review pool is global, not session-scoped.
    """
    now = int(time.time())
    cutoff = max(floor_ts, now - max(1, window_s))
    rows = conn.execute(
        "SELECT role, content, created_at, session_id "
        "FROM dialog_messages "
        "WHERE created_at > ? "
        "ORDER BY created_at ASC",
        (cutoff,),
    ).fetchall()
    if not rows:
        return ("", cutoff, 0)
    lines: list[str] = []
    char_count = 0
    high_water = cutoff
    for r in rows:
        body = r["content"] or ""
        # Cap each turn at 1.5KB so a single noisy tool_result doesn't
        # blow the prompt budget. Most class-level signals are short.
        if len(body) > 1500:
            body = body[:1500] + "…"
        char_count += len(body)
        high_water = max(high_water, int(r["created_at"]))
        sid = (r["session_id"] or "?")[-6:]
        lines.append(f"[{r['role']} @{sid}]\n{body}\n")
    return ("\n".join(lines), high_water, char_count)


def run_shadow_pass(force: bool = False) -> str:
    """Execute one shadow pass synchronously. Used by the daemon AND by
    the MCP tool for manual triggering / testing.

    Returns a short status string for observability:
      - 'disabled'        — env knob off and not forced
      - 'no_window'       — no fresh dialog since last cursor
      - 'too_short'       — window exists but < MIN_CHARS
      - 'spawned task_id=…' — evaluator child launched
      - 'spawn_error: …'  — spawn() rejected (budget cap, etc)
    """
    if SHADOW_REVIEW_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    floor = _last_shadow_ts(conn)
    dump, high_water, n_chars = _collect_window(
        conn, floor, SHADOW_REVIEW_WINDOW_S,
    )
    if n_chars == 0:
        _record_shadow_pass(conn, high_water, "no_window")
        return "no_window"
    if n_chars < SHADOW_REVIEW_MIN_CHARS:
        _record_shadow_pass(conn, high_water, "too_short")
        return "too_short"

    full_prompt = SHADOW_REVIEW_PROMPT + dump

    # Late import — spawn module imports identity / config; importing it
    # at module load time would create cycles.
    from .tools.spawn import spawn  # type: ignore
    try:
        result = spawn(
            prompt=full_prompt,
            visible=False,
            capture_output=True,
            permission_mode="auto",
            role="shadow_observer",
            write_origin="shadow_review",
            slim=True,
            extra_allowed_tools=(
                "mcp__thread-keeper__skill_manage,"
                "mcp__thread-keeper__skill_list,"
                "mcp__thread-keeper__mark_skill_materialized,"
                "Read,Write"
            ),
        )
    except Exception as e:
        _record_shadow_pass(conn, high_water, f"spawn_error: {e}")
        return f"spawn_error: {e}"

    _record_shadow_pass(conn, high_water, str(result)[:200])
    return str(result)


def _serve_loop() -> None:
    """Daemon body. Sleep → tick → sleep, until process dies."""
    while True:
        try:
            run_shadow_pass()
        except Exception:
            logger.debug("shadow_review tick failed", exc_info=True)
        time.sleep(SHADOW_REVIEW_INTERVAL_S)


def start_shadow_daemon() -> None:
    """Idempotent daemon starter. Honors env: no-op when
    SHADOW_REVIEW_INTERVAL_S<=0 so tests stay quiet.

    CRITICAL: only the parent mp process runs this daemon. Spawned slim
    children DO start their own MCP server (so they can call tools), and
    each MCP server in turn calls _ensure_session() which would start
    yet another shadow daemon — that one tries to spawn its own
    evaluator children, which themselves spawn more shadows, etc.
    A recursive spawn cascade.

    We tell parent-vs-child by SEMANTIC_AVAILABLE: parents load the
    embedding model and have it True; slim children get NO_EMBEDDINGS=1
    injected by spawn() so they're False. Same gating that search_proxy
    uses for the symmetric reason.
    """
    global _started
    if _started:
        return
    if SHADOW_REVIEW_INTERVAL_S <= 0:
        return
    from .config import SEMANTIC_AVAILABLE
    if not SEMANTIC_AVAILABLE:
        return  # slim child: don't fire shadow review from here
    t = threading.Thread(
        target=_serve_loop, name="shadow_review", daemon=True,
    )
    t.start()
    _started = True
