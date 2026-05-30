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
from .helpers import alive
from . import identity

logger = logging.getLogger(__name__)

_started = False


# The shadow prompt is what the spawned evaluator child sees. It encodes
# the class-vs-incident decision rubric inline so the child doesn't need
# to (and can't, in slim mode) load the ai-memory-learning-loop skill.
from .i18n import SHADOW_CLASS_SIGNAL_EXAMPLES
from .review_prompts import POSITIVE_EXAMPLES

SHADOW_REVIEW_PROMPT = f"""\
You are a SHADOW LEARNING OBSERVER for thread-keeper. You read a slice
of recent dialog from across ALL agent sessions on this machine and
decide whether any CLASS-LEVEL learning emerged that's worth a durable
skill.

CLASS-LEVEL signals (materialize):
{SHADOW_CLASS_SIGNAL_EXAMPLES}\
- a debugging insight that generalizes beyond the specific bug
- a workflow rule the user stated as policy
- a corrected misunderstanding (existing skill is wrong/incomplete)
- a recovery / cleanup procedure for flaky infra (the FIX outlives the
  incident even when the symptom was env-specific)

NOT class-level (skip):
- one-off task descriptions
- session-transient confusion that resolved itself
- the user asking what something is
- you complimenting yourself or summarizing what just happened
- GENUINELY transient env errors with no durable rule ("rebooted, fixed",
  "wrong dir, fixed"). NOTE: this is narrower than it sounds — see the
  POSITIVE_EXAMPLES block below before defaulting to SKIP.

{POSITIVE_EXAMPLES}

PROCEDURE
1. Read the dialog window below.
2. If nothing class-level emerges → output exactly `SKIP: <one-line reason>` and stop.
3. If class-level learning is present:
   a. PRIMARY: call `mcp__thread-keeper__lesson_append(title, body, summary, source='shadow')`
      to write into ~/.threadkeeper/lessons.md (shared by every CLI).
      - title: lowercase-hyphens slug describing a CLASS of task, not the incident
      - body: markdown rationale + procedure
      - summary: optional one-line TL;DR
   b. OPTIONAL: also call `mcp__thread-keeper__skill_manage(...)` to mirror
      SKILL.md into every configured skills root when frontmatter
      auto-triggering adds value beyond the lesson alone.
   c. Output `MATERIALIZED: <slug>` on success.

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


def _running_shadow_children(conn: sqlite3.Connection) -> list[str]:
    """Return running shadow-observer task ids, refreshing dead rows.

    This is a machine-wide single-flight guard: every foreground
    thread-keeper process shares the same DB, so even if multiple client MCP
    servers have a shadow daemon, only one evaluator child should run at a
    time. Dead pids are marked ended before counting.
    """
    try:
        rows = conn.execute(
            "SELECT id, pid FROM tasks "
            "WHERE ended_at IS NULL "
            "AND prompt LIKE 'You are a SHADOW LEARNING OBSERVER%'"
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


# Opening lines of every prompt we ourselves inject into a spawned
# child. When a child writes its conversation to ~/.claude/projects/
# the parent ingests it back into dialog_messages — without this filter
# the next shadow pass picks up its OWN prior child's reasoning as the
# "recent dialog" and SKIPs ("dialog window contains only shadow-observer
# task framing"). Also catches the close_thread auto-review child whose
# prompt is built around "You are reviewing closed thread <T-code>".
#
# Match is on the FIRST 80 chars of the very first user message of a
# session, so we exclude every message of that session (not just the
# prompt itself — slim children's broadcasts, tool_results, and SKIP
# verdicts also pollute the window).
_INTERNAL_PROMPT_PREFIXES: tuple[str, ...] = (
    "You are a SHADOW LEARNING OBSERVER",
    "You are reviewing closed thread",
    "You are a PROBE RUNNER",
)


# Lines starting with these markers carry no semantic signal for
# class-level learning — they're verbose adapter-side renderings of
# tool_use / tool_result blocks (file dumps, shell output, search
# results). The "clean context" rule: exclude prior-turn tool messages
# from the review summary so the fork sees a clean context. We keep
# `[thinking]` blocks — those ARE signal (chain-of-thought often
# contains the rule being learned).
_NOISE_LINE_PREFIXES: tuple[str, ...] = (
    "[tool_result]",
    "[tool_call]",
    "[tool_use]",
)


def _strip_tool_noise(text: str) -> str:
    """Drop adapter-prefixed tool_result / tool_call lines from a message.

    Returns the cleaned text. If every line in the message was a tool
    artifact, returns the empty string — caller can decide to skip the
    row entirely (zero-information content).
    """
    if not text:
        return text
    # Fast path: no markers → no work
    if not any(p in text for p in _NOISE_LINE_PREFIXES):
        return text
    kept: list[str] = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if any(stripped.startswith(p) for p in _NOISE_LINE_PREFIXES):
            continue
        kept.append(line)
    return "\n".join(kept).strip("\n")


def _collect_window(conn: sqlite3.Connection,
                    floor_ts: int,
                    window_s: int) -> tuple[str, int, int]:
    """Pull dialog messages newer than max(floor_ts, now-window_s),
    excluding any session whose opening user prompt is one of our own
    internal spawn prompts (shadow-observer or close_thread reviewer).

    Returns (dump_text, high_water_ts, char_count).
      - dump_text: human-readable rendering ready for the shadow prompt
      - high_water_ts: largest created_at seen (== floor for next tick)
      - char_count: total visible char length (input to MIN_CHARS guard)

    Mixes all NON-internal active sessions — that's the point. The
    shadow agent's review pool is global across the user's real
    conversations, not the chatter of internal review children.
    """
    now = int(time.time())
    cutoff = max(floor_ts, now - max(1, window_s))
    # Per-prefix `substr(content,1,N) = ?` is friendlier to SQLite's
    # planner than chained `LIKE 'X%' OR LIKE 'Y%'` (no LIKE_PATTERN
    # compile, exact prefix bytewise). N = max prefix length.
    prefix_clauses = " OR ".join(
        ["substr(content, 1, ?) = ?"] * len(_INTERNAL_PROMPT_PREFIXES)
    )
    prefix_params: list = []
    for p in _INTERNAL_PROMPT_PREFIXES:
        prefix_params.extend([len(p), p])
    rows = conn.execute(
        "SELECT role, content, created_at, session_id "
        "FROM dialog_messages "
        "WHERE created_at > ? "
        "  AND session_id NOT IN ("
        "    SELECT DISTINCT session_id FROM dialog_messages "
        f"    WHERE role = 'user' AND ({prefix_clauses})"
        "  ) "
        "ORDER BY created_at ASC",
        (cutoff, *prefix_params),
    ).fetchall()
    if not rows:
        return ("", cutoff, 0)
    lines: list[str] = []
    char_count = 0
    high_water = cutoff
    for r in rows:
        body = _strip_tool_noise(r["content"] or "")
        if not body:
            # Whole turn was tool noise — skip but still advance the
            # cursor (we don't want to re-evaluate this row next pass).
            high_water = max(high_water, int(r["created_at"]))
            continue
        # Cap each turn at 1.5KB so a single noisy block doesn't blow
        # the prompt budget. Most class-level signals are short.
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

    running = _running_shadow_children(conn)
    if running:
        outcome = f"shadow_child_running n={len(running)}"
        # Do not advance high-water. Re-evaluate this window when the current
        # evaluator exits instead of dropping potentially useful dialog.
        _record_shadow_pass(conn, floor, outcome)
        return outcome

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
                "mcp__thread-keeper__lesson_append,"
                "mcp__thread-keeper__lesson_list,"
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

    CRITICAL: only the user-facing parent process runs this daemon.
    Spawned children start their own MCP server so they can call tools; if
    that server starts shadow_review too, it can recursively spawn more
    observer children.

    We use an explicit spawn marker / write-origin guard first, then keep
    the semantic guard as a second belt for older slim children.
    """
    global _started
    if _started:
        return
    if SHADOW_REVIEW_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED, SEMANTIC_AVAILABLE
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    if not SEMANTIC_AVAILABLE:
        return  # slim child: don't fire shadow review from here
    t = threading.Thread(
        target=_serve_loop, name="shadow_review", daemon=True,
    )
    t.start()
    _started = True
