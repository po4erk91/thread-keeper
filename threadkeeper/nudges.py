"""Counter-driven nudge logic.

`memory_nudge_interval` / `skill_nudge_interval` env knobs — when N
mutating events have passed in this session since the last 'save
event' (memory or skill), surface an active nudge in brief() asking
the agent to consolidate.

Unlike spawn_hint and skill_hint (passive observation of state), these
nudges are turn-counter-driven: every mutating tool emits an event, the
counter walks forward, and when it crosses a threshold the surface
escalates from soft → hard → demanding.

Public:
    compute_memory_nudge(conn, session_id) -> Optional[str]
        Returns the nudge text to embed in brief(), or None if quiet.
    compute_skill_nudge(conn, session_id) -> Optional[str]
        Same for skill consolidation.
    auto_review_should_fire(conn, session_id) -> Optional[str]
        Returns a thread_id IF auto-review should spawn now (rich closed
        thread + threshold crossed + AUTO_REVIEW_ENABLED), else None.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

from .config import (
    MEMORY_NUDGE_INTERVAL,
    SKILL_NUDGE_INTERVAL,
    AUTO_REVIEW_ENABLED,
)


# Event kinds that count as "memory save" — emitting any of these resets
# the memory-nudge counter.
_MEMORY_RESET_KINDS = (
    "open_thread",
    "close_thread",
    "note:insight",
    "note:move",
    "core_set",
    "verbatim_user",
    "concept_register",
    "distill",
    "memory_save",
)

# Event kinds that count as "skill save" — emitting any of these resets
# the skill-nudge counter.
_SKILL_RESET_KINDS = (
    "skill_create",
    "skill_edit",
    "skill_patch",
    "skill_write_file",
    "skill_materialized",
)

# Bookkeeping/automatic events that are NOT agent turns and must never
# inflate the memory/skill nudge counters. These are byproducts of the
# system itself, not "work done without a save":
#   thread_hint_shown  — logged by render_brief when the open-thread nudge
#                        is surfaced (once per session).
#   shadow_review_pass — cursor mark written by the shadow-review daemon
#                        on a tick; non-deterministic, daemon-driven.
# Counting either would make a nudge fire a turn early (and made
# test_skill_nudge_soft_at_threshold flaky). They are unioned into the
# exclude set in _count_events_since, NOT added to the *_RESET_KINDS sets,
# so they neither reset the counter nor count toward it.
_NONCOUNTING_KINDS = (
    "thread_hint_shown",
    "shadow_review_pass",
)


def _last_reset_event_id(conn: sqlite3.Connection, session_id: str,
                         kinds: tuple[str, ...]) -> int:
    """Return MAX(events.id) for this session matching any reset-kind, or 0."""
    if not session_id or not kinds:
        return 0
    placeholders = ",".join("?" * len(kinds))
    row = conn.execute(
        f"SELECT COALESCE(MAX(id), 0) m FROM events "
        f"WHERE session_id = ? AND kind IN ({placeholders})",
        (session_id, *kinds),
    ).fetchone()
    if row is None:
        return 0
    return row["m"] if hasattr(row, "keys") else row[0]


def _count_events_since(conn: sqlite3.Connection, session_id: str,
                        since_id: int,
                        exclude_kinds: tuple[str, ...]) -> int:
    """Count events for session with id > since_id whose kind is NOT in
    exclude_kinds. These are the "non-save" turns between the last save
    and now. Bookkeeping kinds (_NONCOUNTING_KINDS) are always excluded on
    top of the caller's exclude_kinds — they are not agent turns."""
    if not session_id:
        return 0
    exclude_kinds = tuple(exclude_kinds) + _NONCOUNTING_KINDS
    if exclude_kinds:
        placeholders = ",".join("?" * len(exclude_kinds))
        row = conn.execute(
            f"SELECT COUNT(*) c FROM events "
            f"WHERE session_id = ? AND id > ? "
            f"AND kind NOT IN ({placeholders})",
            (session_id, since_id, *exclude_kinds),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) c FROM events "
            "WHERE session_id = ? AND id > ?",
            (session_id, since_id),
        ).fetchone()
    if row is None:
        return 0
    return row["c"] if hasattr(row, "keys") else row[0]


def _has_rich_thread(conn: sqlite3.Connection,
                     min_notes: int = 3) -> bool:
    """True if there's at least one active-or-closed thread with ≥ min_notes
    notes total. Used by memory-nudge — there's something worth saving."""
    try:
        row = conn.execute(
            "SELECT t.id "
            "FROM threads t "
            "WHERE t.state IN ('active','closed') "
            "  AND (SELECT COUNT(*) FROM notes n WHERE n.thread_id=t.id) >= ? "
            "LIMIT 1",
            (min_notes,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _find_rich_pending_thread(conn: sqlite3.Connection,
                              within_seconds: int = 86400) -> Optional[str]:
    """Find the richest closed thread that hasn't been materialized into a
    skill yet. Returns thread_id, or None.

    Rich = ≥5 notes, ≥2 of kind 'insight' or 'move'. Recency: closed within
    `within_seconds`. Suppressed when a 'skill_materialized' event already
    exists for the thread.
    """
    now = int(time.time())
    try:
        row = conn.execute(
            "SELECT t.id, "
            "  (SELECT COUNT(*) FROM notes n WHERE n.thread_id=t.id) AS n_total, "
            "  (SELECT COUNT(*) FROM notes n WHERE n.thread_id=t.id "
            "   AND n.kind IN ('insight','move')) AS n_rich "
            "FROM threads t "
            "WHERE t.state='closed' AND t.last_touched_at > ? "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM events e "
            "    WHERE e.kind='skill_materialized' AND e.target=t.id"
            "  ) "
            "  AND (SELECT COUNT(*) FROM notes n WHERE n.thread_id=t.id) >= 5 "
            "  AND (SELECT COUNT(*) FROM notes n WHERE n.thread_id=t.id "
            "       AND n.kind IN ('insight','move')) >= 2 "
            "ORDER BY t.last_touched_at DESC LIMIT 1",
            (now - within_seconds,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return row["id"] if hasattr(row, "keys") else row[0]


def compute_memory_nudge(conn: sqlite3.Connection,
                         session_id: str) -> Optional[str]:
    """Counter-driven memory consolidation nudge. Fires when this session's
    event counter has crossed MEMORY_NUDGE_INTERVAL since the last memory
    save AND there's a rich thread worth saving.

    Returns the multi-line nudge text (to be embedded in brief()), or None.
    """
    if MEMORY_NUDGE_INTERVAL <= 0:
        return None
    if not session_id:
        return None
    last_id = _last_reset_event_id(conn, session_id, _MEMORY_RESET_KINDS)
    n_since = _count_events_since(conn, session_id, last_id,
                                  _MEMORY_RESET_KINDS)
    if n_since < MEMORY_NUDGE_INTERVAL:
        return None
    if not _has_rich_thread(conn, min_notes=3):
        return None
    if n_since >= 2 * MEMORY_NUDGE_INTERVAL:
        # demanding
        return (
            f"memory_nudge n_since={n_since} ⚠️ overdue=2x\n"
            f"  → ⚠️ {n_since} events without a memory save "
            f"(threshold was {MEMORY_NUDGE_INTERVAL}). MUST consolidate "
            f"next: pick richest thread, write insight-note OR "
            f"core_set()/verbatim_user() on the durable signal. "
            f"Continuing without save = losing the work."
        )
    # soft
    return (
        f"memory_nudge n_since={n_since} target=memory "
        f"threshold={MEMORY_NUDGE_INTERVAL}\n"
        f"  → {n_since} events since last memory save. CONSOLIDATE: pick "
        f"the most active thread, write a note(kind='insight') with what "
        f"crystallized, or core_set() the durable lesson. Don't let "
        f"context evaporate."
    )


def compute_skill_nudge(conn: sqlite3.Connection,
                        session_id: str) -> Optional[str]:
    """Counter-driven skill consolidation nudge. Fires when this session's
    event counter has crossed SKILL_NUDGE_INTERVAL since the last skill
    save AND there's a rich closed thread without a prior skill_materialized
    event.
    """
    if SKILL_NUDGE_INTERVAL <= 0:
        return None
    if not session_id:
        return None
    last_id = _last_reset_event_id(conn, session_id, _SKILL_RESET_KINDS)
    n_since = _count_events_since(conn, session_id, last_id,
                                  _SKILL_RESET_KINDS)
    if n_since < SKILL_NUDGE_INTERVAL:
        return None
    if _find_rich_pending_thread(conn) is None:
        return None
    if n_since >= 2 * SKILL_NUDGE_INTERVAL:
        return (
            f"skill_nudge n_since={n_since} ⚠️ overdue=2x\n"
            f"  → ⚠️ {n_since} events without skill update "
            f"(threshold was {SKILL_NUDGE_INTERVAL}). MUST act next: "
            f"materialize the richest closed thread via "
            f"review_thread(..., mode='auto') OR patch the most-relevant "
            f"existing skill via skill_manage(action='patch')."
        )
    return (
        f"skill_nudge n_since={n_since} target=skill "
        f"threshold={SKILL_NUDGE_INTERVAL}\n"
        f"  → {n_since} events since last skill materialize. CHECK: any "
        f"closed thread rich enough (≥5 notes, ≥2 insight/move)? If yes → "
        f"review_thread(thread_id, focus='skills', mode='auto') OR "
        f"skill_manage(action='patch', ...)."
    )


def compute_thread_nudge(conn: sqlite3.Connection,
                         session_id: str) -> Optional[str]:
    """Open-thread nudge for clients WITHOUT a UserPromptSubmit hook
    (Claude Desktop, Codex, VS Code). On hook-capable CLIs (Claude Code,
    Gemini, Copilot) the `tk-thread-nudge.sh` hook covers this, and the
    SessionStart hook sets THREADKEEPER_BRIEF_NO_THREAD_NUDGE so render_brief
    suppresses it — so in practice this only surfaces when the agent calls
    brief() directly, which is exactly what hook-less clients are instructed
    to do at session start.

    Fires once per session, while the session has not yet opened a thread.
    Suppressed as soon as an `open_thread` event exists for the session, or
    after one `thread_hint_shown` event (logged by render_brief when shown).
    Unlike the counter-driven memory/skill nudges, this has no activity
    threshold: the whole point is to remind at the very first brief().
    """
    if not session_id:
        return None
    try:
        if conn.execute(
            "SELECT 1 FROM events WHERE session_id=? AND kind='open_thread' "
            "LIMIT 1",
            (session_id,),
        ).fetchone():
            return None
        if conn.execute(
            "SELECT 1 FROM events WHERE session_id=? AND kind='thread_hint_shown' "
            "LIMIT 1",
            (session_id,),
        ).fetchone():
            return None
    except sqlite3.OperationalError:
        return None
    return (
        "thread_hint: no open_thread yet this session\n"
        "  → if this conversation is a substantive topic (debugging, a "
        "feature, a multi-step task), open_thread(question) now — then "
        "note(thread_id, ..., kind='insight'|'move') as decisions land and "
        "close_thread(thread_id, outcome) when it resolves. Skip if this is "
        "a trivial one-off."
    )


def auto_review_should_fire(conn: sqlite3.Connection,
                            session_id: str,
                            force: bool = False) -> Optional[str]:
    """Decide whether to fire a background review NOW.

    Returns the thread_id of the richest pending closed thread (≥5 notes,
    ≥2 insight/move, no prior skill_materialized) if all of:
      - AUTO_REVIEW_ENABLED is true (skipped when force=True)
      - skill-nudge counter is at or past SKILL_NUDGE_INTERVAL (skipped
        when force=True)
      - a rich pending thread exists

    Otherwise None.
    """
    if not force:
        if not AUTO_REVIEW_ENABLED:
            return None
        if SKILL_NUDGE_INTERVAL <= 0:
            return None
        if not session_id:
            return None
        last_id = _last_reset_event_id(conn, session_id, _SKILL_RESET_KINDS)
        n_since = _count_events_since(conn, session_id, last_id,
                                      _SKILL_RESET_KINDS)
        if n_since < SKILL_NUDGE_INTERVAL:
            return None
    return _find_rich_pending_thread(conn)
