"""Candidate-reviewer daemon — closes the extract → SKILL.md gap.

Architecture position relative to the other four loops:

  extract_daemon       — harvests heuristic candidates from
                         dialog_messages → extract_candidates.status='pending'
  candidate_reviewer   — THIS LOOP. Periodically wakes, reads the
                         pending queue, spawns an LLM child to decide
                         per candidate: SKILL.create / SKILL.patch /
                         NOTE / VERBATIM / REJECT.
  shadow_review        — parallel path: scans dialog_messages directly
                         for class-level signals (doesn't touch
                         extract_candidates).
  close_thread auto-review — fires on thread close with rich notes,
                         materializes from notes dump.
  curator              — audits the EXISTING library, doesn't generate
                         new entries.

Before this loop landed, pending candidates accumulated indefinitely:
extract harvested them, but materialization required the agent to call
`accept_candidate` / `skill_manage` manually — which the audit on
2026-05-16 confirmed agents rarely do. This daemon is the missing
consumer.

Trigger: every CANDIDATE_REVIEW_INTERVAL_S seconds (0 = off,
recommended 3600 = 1h — extract typically adds ~10 candidates/h so
hourly review keeps the queue from backing up).

Hardstops:
- Below CANDIDATE_REVIEW_MIN candidates → skip the spawn; not enough
  signal to justify the cost.
- Slim children must NOT start this daemon (cascade prevention via
  SEMANTIC_AVAILABLE).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from .config import (
    CANDIDATE_REVIEW_FLUSH_AGE_S,
    CANDIDATE_REVIEW_INTERVAL_S,
    CANDIDATE_REVIEW_MIN,
)
from .db import get_db
from .helpers import daemon_sleep, single_flight_lock
from . import daemon_state, identity

logger = logging.getLogger(__name__)

_started = False

CANDIDATE_REVIEW_PROMPT_PREFIX = "You are a CANDIDATE REVIEWER"

CANDIDATE_REVIEW_PROMPT = (
    CANDIDATE_REVIEW_PROMPT_PREFIX
    + """ for thread-keeper's extract queue.

The extract daemon harvests heuristic candidates from agent dialog
into the `extract_candidates` table (status='pending'). Your job is to
decide each candidate's fate. Inventory below lists every pending
candidate with its kind heuristic, rationale, and content snippet,
PLUS the list of recently-active skills (so you can prefer
PATCH-existing over CREATE-new).

PROCEDURE — for each candidate (or coherent cluster of candidates),
choose exactly one action:

  1. SKILL.create — candidate (or merged cluster of 2+ related ones)
     expresses a class-level rule worth a durable SKILL.md. Call:
        skill_manage(
            action='create',
            name='<kebab-case-class-name>',
            description='<frontmatter description triggering the skill>',
            content='<body markdown>'
        )
     Then for every candidate id that fed into this skill, call:
        reject_candidate(id=<id>, reason='materialized as skill:<name>')
     The skill IS the materialization; the candidates are no longer
     pending business.

  2. SKILL.patch — candidate refines a skill listed under RECENTLY
     ACTIVE SKILLS (or one you'd consult by name). Call:
        skill_manage(
            action='patch', name=..., old_string=..., new_string=...
        )
     Then reject the underlying candidate(s).

  3. SKILL.write_file — candidate is detail worth keeping under an
     existing skill but not in the main SKILL.md. Add as a
     references/ sub-file:
        skill_manage(
            action='write_file', name=..., sub_path='references/<topic>.md',
            content=...
        )
     Then reject the underlying candidate.

  4. NOTE — per-incident decision worth keeping but NOT skill-level
     (one-off observation, in-flight bug repro). Requires a
     thread_id; if no fitting open thread exists, skip and leave
     pending. Call:
        accept_candidate(id=..., target_kind='note', thread_id=...)

  5. VERBATIM — user quote / statement worth preserving verbatim in
     brief(). Call:
        accept_candidate(id=..., target_kind='verbatim')

  6. REJECT — false positive that slipped past extract's noise
     filters (system prompt fragment, log dump, etc.). Call:
        reject_candidate(id=..., reason='<one-line>')

DECISION RULES:

- PREFER PATCH over CREATE. If the candidate territory overlaps with
  a listed recent skill, patch that one. New skills that overlap
  existing ones pollute the auto-trigger surface.
- MERGE before CREATE. If 2-5 candidates form a coherent cluster
  (same domain, same rule, paraphrased), create ONE skill that
  references all of them. Don't spawn N narrow skills.
- LIMIT 2 new skills per pass. If the queue suggests more, pick the
  2 highest-leverage clusters; leave the rest pending for next pass.
- NEVER touch [PROTECTED] skills in the active-skills list — those
  are pinned or foreground-authored. If a candidate seems to belong
  to a protected skill, REJECT the candidate (don't patch protected).
- For action=NOTE without a clear thread_id — DON'T guess. Leave the
  candidate pending; an agent may pick the right thread later.
- Skip-and-leave-pending IS a valid action — but ONLY when no other
  action fits, not as a default. Most candidates should resolve to
  one of REJECT / NOTE / VERBATIM / SKILL.

OUTPUT — write a one-paragraph summary at the end of your run:
   "Processed N candidates: K created skills, P patched skills,
   M notes, V verbatim, R rejected, S left-pending. Reason for any
   created skill: ..."

"""
)


# ──────────────────────────────────────────────────────────────────────
# Pure functions: cursor + inventory collection
# ──────────────────────────────────────────────────────────────────────

def _last_review_ts(conn: sqlite3.Connection) -> int:
    """High-water timestamp of the most recent candidate review pass."""
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='candidate_review_pass' "
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


def _record_review_pass(conn: sqlite3.Connection, ts: int,
                       outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, "
            "created_at) VALUES (?, 'candidate_review_pass', ?, ?, ?)",
            (identity._session_id or "", str(ts), outcome[:300],
             int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("candidate_reviewer: record_pass failed",
                     exc_info=True)


def _pass_due(conn: sqlite3.Connection, now_t: int) -> bool:
    last = _last_review_ts(conn)
    return last <= 0 or now_t >= last + int(CANDIDATE_REVIEW_INTERVAL_S)


def _format_candidate(row: dict) -> str:
    """One inventory line per pending candidate."""
    snip = (row.get("content") or "")[:300].replace("\n", " ")
    if len(row.get("content") or "") > 300:
        snip += "…"
    return (
        f"  #{row['id']} kind={row['kind']} "
        f"cid={(row.get('source_cid') or '-')[:8]} "
        f"why={(row.get('rationale') or '?')[:60]}\n"
        f"    content: {snip}"
    )


def _active_skills_dump(conn: sqlite3.Connection, limit: int = 15,
                       window_days: int = 14) -> str:
    """Same active-update bias context the auto-review fork gets.
    Imported logic from tools.skills but duplicated to avoid circular
    import at daemon load time."""
    now = int(time.time())
    cutoff = now - window_days * 86400
    try:
        rows = conn.execute(
            "SELECT name, use_count, pinned, created_by_origin, "
            "       MAX(COALESCE(last_used_at,0), "
            "           COALESCE(last_viewed_at,0), "
            "           COALESCE(last_patched_at,0)) AS last_active "
            "FROM skill_usage WHERE state='active' AND last_active > ? "
            "ORDER BY last_active DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return ""
    if not rows:
        return ""
    lines = [
        "RECENTLY ACTIVE SKILLS (prefer PATCH over CREATE — see rules):",
    ]
    for r in rows:
        protected = ""
        if r["pinned"] or r["created_by_origin"] == "foreground":
            protected = " [PROTECTED]"
        lines.append(
            f"  - {r['name']}{protected} (uses={r['use_count']})"
        )
    return "\n".join(lines) + "\n\n"


def _collect_pending(conn: sqlite3.Connection) -> tuple[str, int]:
    """Build the inventory the reviewer child will read.

    Returns (dump_text, n_pending). Only candidates within last 30
    days are surfaced — older = stale, likely already overtaken by
    fresh dialog.
    """
    now = int(time.time())
    stale_cutoff = now - 30 * 86400
    try:
        rows = conn.execute(
            "SELECT id, kind, source_uuid, source_cid, content, "
            "       rationale, created_at "
            "FROM extract_candidates "
            "WHERE status='pending' AND created_at > ? "
            "ORDER BY created_at DESC",
            (stale_cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        return ("", 0)
    if not rows:
        return ("", 0)
    parts: list[str] = [f"PENDING CANDIDATES (n={len(rows)})\n"]
    parts.extend(_format_candidate(dict(r)) for r in rows)
    return ("\n".join(parts), len(rows))


def _oldest_pending_ts(conn: sqlite3.Connection) -> int:
    """created_at of the oldest surfaced (non-stale) pending candidate, or 0."""
    stale_cutoff = int(time.time()) - 30 * 86400
    try:
        row = conn.execute(
            "SELECT MIN(created_at) FROM extract_candidates "
            "WHERE status='pending' AND created_at > ?",
            (stale_cutoff,),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row and row[0] else 0


def _running_reviewer_children(conn: sqlite3.Connection) -> list[str]:
    """Running candidate-review task ids, reaping dead rows.

    Candidate review consumes one global queue. Two reviewers launched from
    different foreground MCP servers will read the same candidates and burn
    memory doing duplicate work, so this loop is machine-wide single-flight.
    """
    from .helpers import alive
    try:
        rows = conn.execute(
            "SELECT id, pid FROM tasks WHERE ended_at IS NULL "
            "AND prompt LIKE ?",
            (CANDIDATE_REVIEW_PROMPT_PREFIX + "%",),
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


def _review_spawn_lock():
    """Cross-process guard for check-running-then-spawn.

    The tasks-table running check is not atomic across foreground MCP
    processes. A short file lock prevents two daemon ticks from both observing
    no reviewer and spawning duplicate children for the same pending queue.
    """
    return single_flight_lock("candidate-reviewer")


# ──────────────────────────────────────────────────────────────────────
# Synchronous pass + daemon loop
# ──────────────────────────────────────────────────────────────────────

def run_review_pass(force: bool = False, *, scheduled: bool = False) -> str:
    """Execute one candidate review pass synchronously.

    Returns a short status string:
      - 'disabled'             — env knob off and not forced
      - 'not_due'              — checked recently; interval high-water not due
      - 'below_threshold n=X'  — fewer than CANDIDATE_REVIEW_MIN
                                 pending; skip the spawn
      - 'spawned task_id=…'    — reviewer child launched
      - 'spawn_error: …'       — spawn() rejected (budget cap, etc.)
    """
    if CANDIDATE_REVIEW_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    now = int(time.time())
    if not force and not _pass_due(conn, now):
        _record_review_pass(conn, _last_review_ts(conn), "not_due")
        return "not_due"
    if not daemon_state.claim_pass(
        "candidate_review", CANDIDATE_REVIEW_INTERVAL_S, scheduled=scheduled,
        conn=conn, now=now,
    ):
        _record_review_pass(conn, _last_review_ts(conn), "not_due")
        return "not_due"
    with _review_spawn_lock() as locked:
        if not locked:
            return "candidate_review_running n=1 (single-flight lock)"

        running = _running_reviewer_children(conn)
        if running:
            out = f"candidate_review_running n={len(running)} (single-flight)"
            _record_review_pass(conn, now, out)
            return out

        inventory, n_pending = _collect_pending(conn)
        if n_pending < CANDIDATE_REVIEW_MIN:
            flush_age = float(CANDIDATE_REVIEW_FLUSH_AGE_S or 0)
            oldest = _oldest_pending_ts(conn)
            aged_out = (
                n_pending > 0 and flush_age > 0
                and oldest and now - oldest >= flush_age
            )
            if not aged_out:
                _record_review_pass(
                    conn, now,
                    f"below_threshold pending={n_pending} "
                    f"min={CANDIDATE_REVIEW_MIN}",
                )
                return f"below_threshold n={n_pending}"
            # Age-flush: a trickle of candidates must not be starved by the
            # min-count gate until the 30-day stale window silently drops
            # them — review the undersized queue anyway.

        active_skills = _active_skills_dump(conn)
        # The harvested candidate `content` snippets are untrusted observed
        # dialog (issue #76) — fence them as data. The active-skills list
        # is our own DB state, so it stays outside the fence.
        from .review_prompts import DATA_FENCE, fence_observed
        full_prompt = (
            CANDIDATE_REVIEW_PROMPT
            + DATA_FENCE + "\n\n"
            + active_skills
            + fence_observed(inventory, "pending candidate snippets")
        )

        from .tools.spawn import spawn  # type: ignore
        try:
            result = spawn(
                prompt=full_prompt,
                visible=False,
                capture_output=True,
                permission_mode="auto",
                role="candidate_reviewer",
                write_origin="candidate_review",
                slim=True,
                # De-privileged (issue #76): path-scoped skill/lesson/
                # candidate tools only — no bare Read/Write. Reference
                # files go through skill_manage(action='write_file').
                extra_allowed_tools=(
                    "mcp__thread-keeper__skill_manage,"
                    "mcp__thread-keeper__skill_list,"
                    "mcp__thread-keeper__accept_candidate,"
                    "mcp__thread-keeper__reject_candidate,"
                    "mcp__thread-keeper__lesson_append,"
                    "mcp__thread-keeper__mark_skill_materialized"
                ),
            )
        except Exception as e:
            _record_review_pass(conn, now, f"spawn_error: {e}")
            return f"spawn_error: {e}"

        _record_review_pass(
            conn, now,
            f"spawned pending={n_pending} :: {str(result)[:140]}",
        )
        return str(result)


def _serve_loop() -> None:
    """Daemon body. Sleep → tick → sleep, until process dies."""
    while True:
        try:
            run_review_pass(scheduled=True)
        except Exception:
            logger.debug("candidate_reviewer tick failed", exc_info=True)
        daemon_sleep(CANDIDATE_REVIEW_INTERVAL_S)


def start_candidate_reviewer_daemon() -> None:
    """Idempotent daemon starter. Uses the same spawned/background child
    cascade prevention as shadow_review / curator / extract."""
    global _started
    if _started:
        return
    if CANDIDATE_REVIEW_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED, SEMANTIC_AVAILABLE
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    if not SEMANTIC_AVAILABLE:
        return
    t = threading.Thread(
        target=_serve_loop, name="candidate_reviewer", daemon=True,
    )
    t.start()
    _started = True
