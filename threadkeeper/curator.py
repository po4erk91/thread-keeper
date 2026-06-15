"""Autonomous Curator — periodic library audit & consolidation.

Where shadow_review LOOKS FOR NEW class-level learning every few
minutes, the Curator REVIEWS THE STORE every few days:

  1. Daemon thread wakes every CURATOR_INTERVAL_S seconds (0 = off).
  2. Collects inventory: every lesson slug + every recently-touched
     skill + usage telemetry.
  3. Spawns slim child with CURATOR_PROMPT + inventory dump.
  4. Child grades each entry, suggests KEEP / PATCH / CONSOLIDATE /
     PRUNE, and writes REPORT-<isodate>.md under CURATOR_REPORTS_DIR.
  5. Parent records `curator_pass` event with high-water timestamp.

Design choices:

  • **Class-first / rubric-based output** — child uses an explicit
    decision matrix (see CURATOR_PROMPT) rather than free-form grading.
  • **Defense-in-depth** — pinned lessons/skills and foreground-origin
    entries are listed in the inventory as PROTECTED so the child knows
    not to touch them.
  • **Scoped toolset** — child gets only lesson_*/skill_*/Read/Write.
    No shell, no web, no spawn. Curator can't sprawl into anything else.
  • **Per-run REPORT.md** — every pass leaves an auditable trail.
  • **Read-only-by-default destructive ops** — Phase 1: child writes a
    REPORT.md with recommendations only. User reviews it and decides
    whether to apply patches/consolidations manually. Future versions
    can flip an env knob to let the curator merge/delete in place.

Why this exists: shadow_review accumulates lessons over weeks. Without
periodic curation, the library grows unbounded with overlapping,
duplicate, or stale content.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from .config import (
    CURATOR_INTERVAL_S,
    CURATOR_MIN_LESSONS,
    CURATOR_REPORTS_DIR,
    CURATOR_DESTRUCTIVE,
)
from .db import get_db
from .helpers import daemon_sleep
from . import identity, lessons

logger = logging.getLogger(__name__)

_started = False


CURATOR_PROMPT = """\
You are an autonomous CURATOR for thread-keeper's lessons + skills
library. You read the inventory below — every lesson slug, every
recently-touched skill, usage telemetry — and decide what to keep,
patch, consolidate, or prune.

Where the shadow_review observer LOOKS FOR new class-level learning,
your role is the inverse: review the EXISTING store for quality, dedup,
and freshness.

OUTPUT: write a REPORT.md to ~/.threadkeeper/curator/REPORT-<isodate>.md
via Write tool. The REPORT.md is your sole user-visible output; the
human reads it later and decides which recommendations to apply.

EVOLVE CANDIDATES — if a lesson or skill reveals an important improvement for
thread-keeper itself (security, privacy, memory leaks, daemon/cost waste,
reliability, roadmap automation, adapter correctness, or a strong workflow
lesson that should change thread-keeper code/docs), create exactly one
candidate for Evolve reviewer by calling:

  evolve_format(
    suggestion="<concrete thread-keeper improvement to audit/turn into issue>",
    rationale="<which lesson/skill exposed it and why it matters>"
  )

Do this sparingly. Do NOT create evolve candidates for ordinary skill-library
maintenance, duplicate cleanup, style nits, or project-specific lessons that do
not improve thread-keeper itself. Also include a short `EVOLVE_CANDIDATE:` line
in the REPORT.md for every candidate you created so the human audit trail shows
why it was filed.

RUBRIC (per-entry decision matrix — answer for every lesson and every
recently-active skill):

  KEEP — entry is class-level, in use, accurate. Note "KEEP: <slug>".

  PATCH — entry is mostly right but missing a step, has outdated
  example, or contradicts something more recent. Quote the exact
  string to change and the replacement. Format:
    PATCH: <slug>
      old: "<exact substring>"
      new: "<replacement>"
      reason: <one line>

  CONSOLIDATE — two or more entries cover overlapping territory and
  would be stronger as one umbrella. Format:
    CONSOLIDATE: <merged-slug>
      merges: <slug-a>, <slug-b>, ...
      keep_in_umbrella: <bullet list of what carries over>
      reason: <one line on why they overlap>

  PRUNE — entry is one-off incident narrative, env-specific transient,
  superseded by a newer entry, or a **FALSE POSITIVE** (auto-created
  by the background-review loop but never validated by actual use).
  Specifically flag as PRUNE:
    • origin=background_review AND use_count=0 AND patches=0 AND
      created >14 days ago → strong false-positive signal: nobody ever
      consulted it, and the agent that created it never came back to
      refine it.
    • SKILL_OUTCOME signals (in the events table) marking the skill
      as 'wrong' more often than 'helped' → user-judgment override.
  Format:
    PRUNE: <slug>
      reason: <one line; note "false_positive" if from the criteria above>

CONCEPTS RUBRIC — if a `## CONCEPTS` section is present below, review it
with the SAME verbs (KEEP / CONSOLIDATE / PRUNE; PATCH rarely applies).
Concepts are abstract regularities the system noticed; they are all
system-generated, so NONE are [PROTECTED] — you may recommend
destructive changes freely. Priorities specific to concepts:
  • CONSOLIDATE first — the concept store is thin and prone to near-
    duplicate descriptions of the same idea. Merging overlapping
    concepts is the highest-value action here. Format:
      CONSOLIDATE_CONCEPT: <kept-id>
        merges: <id-a>, <id-b>
        reason: <one line on the overlap>
  • PRUNE a concept that is `conf=low AND last_evidence >30d_ago` —
    registered once, never corroborated: the concept equivalent of an
    unused background_review skill (false positive). Format:
      PRUNE_CONCEPT: <id>
        reason: <one line; note "false_positive" if low-conf+stale>
  • For a `conf=medium`+ concept with no fresh evidence in 30d, RECOMMEND
    a confidence review (it may be aging out) — note it, don't mutate.

INVENTORY ORDERING — entries marked [PROTECTED] are pinned or
foreground-authored. NEVER suggest PATCH/CONSOLIDATE/PRUNE on those —
only KEEP. Always-OK to RECOMMEND that the user manually review them,
but the curator must not propose destructive changes.

PRIORITY ORDER inside the REPORT.md:
  1. CONSOLIDATE recommendations first (highest leverage — merging two
     overlapping entries clarifies the whole library).
  2. PATCH recommendations next (low-risk, in-place improvements).
  3. PRUNE recommendations last (highest-risk; require explicit human
     confirmation).
  4. KEEP entries summarised at the end as a short list of slugs.

OPEN with a one-paragraph LIBRARY HEALTH summary: total entries,
average use_count, most/least-used skill, oldest untouched entry.

CLOSE with the literal line `CURATOR_PASS_COMPLETE` so the parent
process knows the run finished cleanly.

CONSTRAINTS:
- Do NOT cite internal IDs (T-codes, cids, task IDs) in the REPORT.md.
  Plain prose for the human reader.
- If the inventory is genuinely fine (no patches/consolidations/prunes
  warranted), still write a REPORT.md that says so — the trail matters
  even when nothing changes.
- {DESTRUCTIVE_CLAUSE}

INVENTORY
=========
"""


# ──────────────────────────────────────────────────────────────────────
# Pure functions: cursor, inventory collection
# ──────────────────────────────────────────────────────────────────────

def _last_curator_ts(conn: sqlite3.Connection) -> int:
    """High-water timestamp of the most recent curator pass. Stored in
    `target` of the latest `events.kind='curator_pass'` row so `summary`
    is free for human-readable outcome. Returns 0 when no prior pass."""
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='curator_pass' "
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


def _record_curator_pass(conn: sqlite3.Connection,
                         ts: int,
                         outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, "
            "created_at) VALUES (?, 'curator_pass', ?, ?, ?)",
            (identity._session_id or "", str(ts), outcome[:300],
             int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("curator: failed to record pass", exc_info=True)


def _format_lesson(item: dict) -> str:
    """One inventory line per lesson.

    Lessons aren't pinned in the same way skills are — but lessons
    flagged with `source=foreground` (i.e. user-typed via lesson_append
    in a live session, not auto-spawned) get the PROTECTED marker so
    the curator never proposes destructive changes against them."""
    src = (item.get("source") or "").strip()
    protected = " [PROTECTED]" if src in ("foreground", "user") else ""
    ts = item.get("ts") or 0
    age_d = (int(time.time()) - ts) // 86400 if ts else "?"
    body_preview = (item.get("body") or "")[:200].replace("\n", " ")
    if len(item.get("body") or "") > 200:
        body_preview += "…"
    return (
        f"- LESSON {item['slug']}{protected} "
        f"(source={src or '?'}, age={age_d}d)\n"
        f"    body: {body_preview}"
    )


def _format_skill(row: dict) -> str:
    """One inventory line per recently-touched skill row from
    skill_usage. Foreground-origin and pinned skills are PROTECTED."""
    origin = row.get("created_by_origin") or "?"
    protected = ""
    if row.get("pinned") or origin == "foreground":
        protected = " [PROTECTED]"
    now = int(time.time())
    last_active = max(
        row.get("last_used_at") or 0,
        row.get("last_viewed_at") or 0,
        row.get("last_patched_at") or 0,
        row.get("created_at") or 0,
    )
    age_d = (now - last_active) // 86400 if last_active else "?"
    return (
        f"- SKILL {row['name']}{protected} "
        f"(origin={origin}, uses={row.get('use_count', 0)}, "
        f"views={row.get('view_count', 0)}, "
        f"patches={row.get('patch_count', 0)}, "
        f"last_active={age_d}d_ago, state={row.get('state', '?')})"
    )


def _collect_inventory(conn: sqlite3.Connection) -> tuple[str, int, int]:
    """Build the inventory dump the curator child will read.

    Returns (dump_text, lesson_count, skill_count). The dump format is
    plain text — `_format_lesson` and `_format_skill` produce one line
    per entry, grouped into LESSONS and SKILLS sections.
    """
    # ---- Lessons ----
    lesson_lines: list[str] = []
    n_lessons = 0
    try:
        for item in lessons.iter_lessons():
            lesson_lines.append(_format_lesson(item))
            n_lessons += 1
    except Exception:
        logger.debug("curator: iter_lessons failed", exc_info=True)

    # ---- Skills ----
    skill_lines: list[str] = []
    n_skills = 0
    try:
        rows = conn.execute(
            "SELECT name, created_at, created_by_origin, last_used_at, "
            "last_viewed_at, last_patched_at, use_count, view_count, "
            "patch_count, pinned, state "
            "FROM skill_usage "
            "WHERE state IN ('active', 'stale') "
            "ORDER BY COALESCE(last_used_at, last_viewed_at, "
            "                  last_patched_at, created_at) DESC"
        ).fetchall()
        for r in rows:
            skill_lines.append(_format_skill(dict(r)))
            n_skills += 1
    except sqlite3.OperationalError:
        logger.debug("curator: skill_usage scan failed", exc_info=True)

    parts: list[str] = []
    parts.append(f"## LESSONS (n={n_lessons})\n")
    parts.extend(lesson_lines if lesson_lines else ["(none)"])
    parts.append(f"\n## SKILLS (n={n_skills})\n")
    parts.extend(skill_lines if skill_lines else ["(none)"])

    return ("\n".join(parts), n_lessons, n_skills)


def _collect_concepts(conn: sqlite3.Connection) -> tuple[str, int]:
    """Build the concepts section of the curator inventory.

    Returns (dump_text, concept_count). Empty string when there are no
    concepts. Ordered oldest-evidence-first so the curator sees the
    stalest (most prune-worthy) entries at the top. Each line carries the
    confidence band and days since last corroboration — the two signals
    the curator rubric uses to flag low-confidence/never-corroborated
    concepts as false positives."""
    try:
        rows = conn.execute(
            "SELECT id, description, confidence, registered_at, "
            "last_evidence_at FROM concepts "
            "ORDER BY COALESCE(last_evidence_at, registered_at) ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return "", 0
    if not rows:
        return "", 0
    now_t = int(time.time())
    lines = [f"## CONCEPTS (n={len(rows)})\n"]
    for r in rows:
        last = r["last_evidence_at"] or r["registered_at"]
        age_d = max(0, (now_t - last) // 86400)
        desc = (r["description"] or "").replace("\n", " ")[:200]
        lines.append(
            f"- {r['id']} conf={r['confidence']} "
            f"last_evidence={age_d}d_ago\n"
            f"    {desc}"
        )
    return "\n".join(lines), len(rows)


# ──────────────────────────────────────────────────────────────────────
# Synchronous pass + daemon loop
# ──────────────────────────────────────────────────────────────────────

def run_curator_pass(force: bool = False) -> str:
    """Execute one curator pass synchronously. Used by the daemon AND
    by the MCP tool for manual triggering / testing.

    Returns a short status string for observability:
      - 'disabled'        — env knob off and not forced
      - 'below_threshold' — fewer than CURATOR_MIN_LESSONS lessons; skip
      - 'spawned task_id=…' — curator child launched
      - 'spawn_error: …'  — spawn() rejected
    """
    if CURATOR_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    inventory, n_lessons, n_skills = _collect_inventory(conn)
    now = int(time.time())
    if n_lessons < CURATOR_MIN_LESSONS:
        _record_curator_pass(
            conn, now,
            f"below_threshold lessons={n_lessons} skills={n_skills}",
        )
        return f"below_threshold lessons={n_lessons}"

    # Concepts enrich the review but do NOT lower the lesson threshold —
    # a curator pass is only worth a child spawn when there's a real
    # lesson/skill inventory to audit; concepts ride along.
    concepts_text, n_concepts = _collect_concepts(conn)
    if concepts_text:
        inventory = inventory + "\n\n" + concepts_text

    # Ensure reports dir exists before the child tries to Write into it.
    CURATOR_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Phase-1 default: advisory-only. CURATOR_DESTRUCTIVE=1 promotes
    # the child to "apply your own recommendations directly" mode and
    # widens the allowed-tools list to include skill_manage + lesson_append.
    if CURATOR_DESTRUCTIVE:
        destructive_clause = (
            "DESTRUCTIVE MODE ENABLED. After writing the REPORT.md you "
            "MAY apply your own PATCH / PRUNE / CONSOLIDATE recommendations "
            "directly via skill_manage(action='patch'|'delete'|'write_file') "
            "and lesson_append(...). Always cross-check against the "
            "[PROTECTED] marker — never touch protected entries even in "
            "destructive mode. Apply changes ONLY after the REPORT.md is "
            "written (audit trail first, mutation second)."
        )
        allowed_tools = (
            "mcp__thread-keeper__lesson_list,"
            "mcp__thread-keeper__lesson_get,"
            "mcp__thread-keeper__lesson_append,"
            "mcp__thread-keeper__skill_list,"
            "mcp__thread-keeper__skill_manage,"
            "mcp__thread-keeper__evolve_format,"
            "Read,Write"
        )
    else:
        destructive_clause = (
            "ADVISORY MODE. Do NOT call lesson_append, skill_manage with "
            "action in {create,patch,delete,write_file}, or any other "
            "destructive tool. Your output is the REPORT.md ONLY — the "
            "human reviews and applies changes manually. Flip "
            "THREADKEEPER_CURATOR_DESTRUCTIVE=1 in env when ready to let "
            "the curator apply its own recommendations."
        )
        allowed_tools = (
            "mcp__thread-keeper__lesson_list,"
            "mcp__thread-keeper__lesson_get,"
            "mcp__thread-keeper__skill_list,"
            "mcp__thread-keeper__evolve_format,"
            "Read,Write"
        )

    full_prompt = (
        CURATOR_PROMPT.replace("{DESTRUCTIVE_CLAUSE}", destructive_clause)
        + inventory
        + "\n\n"
        + f"REPORT_PATH = {CURATOR_REPORTS_DIR}/REPORT-"
        f"{time.strftime('%Y%m%dT%H%M%S')}.md\n"
        + "Write the REPORT.md to that exact path."
    )

    from .tools.spawn import spawn  # type: ignore
    try:
        result = spawn(
            prompt=full_prompt,
            visible=False,
            capture_output=True,
            permission_mode="auto",
            role="curator",
            write_origin="curator",
            slim=True,
            extra_allowed_tools=allowed_tools,
        )
    except Exception as e:
        _record_curator_pass(conn, now, f"spawn_error: {e}")
        return f"spawn_error: {e}"

    _record_curator_pass(
        conn, now,
        f"spawned lessons={n_lessons} skills={n_skills} "
        f"concepts={n_concepts} :: {str(result)[:140]}",
    )
    return str(result)


def _serve_loop() -> None:
    """Daemon body. Sleep → tick → sleep, until process dies."""
    while True:
        try:
            run_curator_pass()
        except Exception:
            logger.debug("curator tick failed", exc_info=True)
        daemon_sleep(CURATOR_INTERVAL_S)


def start_curator_daemon() -> None:
    """Idempotent daemon starter. Honors env: no-op when
    CURATOR_INTERVAL_S<=0. Identical cascade-prevention as
    start_shadow_daemon: spawned/background children refuse to start
    the daemon so spawn() doesn't recurse."""
    global _started
    if _started:
        return
    if CURATOR_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED, SEMANTIC_AVAILABLE
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    if not SEMANTIC_AVAILABLE:
        return  # slim child: don't fire curator from here
    t = threading.Thread(
        target=_serve_loop, name="curator", daemon=True,
    )
    t.start()
    _started = True
