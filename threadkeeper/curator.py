"""Autonomous Curator — periodic library audit & consolidation.

Where shadow_review LOOKS FOR NEW class-level learning every few
minutes, the Curator REVIEWS THE STORE every few days:

  1. Daemon thread wakes every CURATOR_INTERVAL_S seconds (0 = off).
  2. Fingerprints lessons, concepts, every tracked/materialized skill body,
     support-file tree, validator result, and mirror state.
  3. Manual duplicate invocations debounce an unchanged inventory; scheduled
     passes still run so web-backed freshness is re-checked every interval.
  4. Writes AUDIT-<isodate>.json: an exhaustive numbered skill manifest with
     consumer validation, link/resource findings, exact duplicate groups, and
     semantic-review candidates.
  5. Spawns a slim child with web research plus the manifest/checklist.
  6. Child reads every full skill, researches current official guidance and
     comparable skills, then chooses KEEP / REPAIR / UPDATE / MERGE / SPLIT /
     DEPRECATE / DELETE / CROSS_LINK / HUMAN_REVIEW.
  7. In destructive mode, parent writes a pre-mutation snapshot before
     spawning the child; child tool calls add tombstones/action telemetry.
  8. Parent records `curator_pass` event with high-water timestamp and
     inventory fingerprint.

Design choices:

  • **Class-first / rubric-based output** — child uses an explicit
    decision matrix (see CURATOR_PROMPT) rather than free-form grading.
  • **Defense-in-depth** — protected lessons/skills are listed in the
    inventory as PROTECTED and delete-class MCP tools refuse them
    server-side unless a foreground writer explicitly forces the action.
  • **Scoped toolset** — child gets library tools, Read/Write, deterministic
    skill_validate, WebSearch/WebFetch, and snapshot restore. No shell/spawn.
  • **Per-run REPORT.md** — every pass leaves an auditable trail.
  • **Destructive-by-default (Phase 2)** — parent first writes a recoverable
    snapshot under CURATOR_REPORTS_DIR/snapshots/<pass-id>. The child writes
    the REPORT.md first (audit trail), then applies its own PATCH / PRUNE /
    CONSOLIDATE directly via lesson_append / lesson_remove / skill_manage, and
    its CONSOLIDATE_CONCEPT / PRUNE_CONCEPT recommendations via concept_manage.
    Set THREADKEEPER_CURATOR_DESTRUCTIVE=0 to revert to advisory REPORT-only.
    [PROTECTED] entries are never mutated; lesson_remove and
    skill_manage(action='delete') enforce that server-side, and
    non-foreground children cannot elevate themselves with force.
    Concepts are all system-generated, so concept_manage needs no such
    guard — every concept is curatable.

Why this exists: shadow_review accumulates lessons over weeks. Without
periodic curation, the library grows unbounded with overlapping,
duplicate, or stale content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time

from .config import (
    CURATOR_INTERVAL_S,
    CURATOR_MIN_LESSONS,
    CURATOR_REPORTS_DIR,
    CURATOR_DESTRUCTIVE,
    CURATOR_MANAGE_FOREGROUND_SKILLS,
    CURATOR_SNAPSHOT_RETENTION,
)
from .db import get_db
from .helpers import daemon_sleep, single_flight_lock
from . import daemon_state, identity, lessons
from .curator_snapshots import (
    PASS_ID_ENV,
    SNAPSHOT_DIR_ENV,
    create_curator_snapshot,
)
from .skill_audit import (
    build_skill_audit,
    format_skill_checklist,
    write_skill_audit_manifest,
)

logger = logging.getLogger(__name__)

_started = False

INVENTORY_FINGERPRINT_KEY = "inventory_sha256"
_INVENTORY_FINGERPRINT_RE = re.compile(
    rf"\b{INVENTORY_FINGERPRINT_KEY}=([0-9a-f]{{64}})\b"
)

_CURATABLE_SKILL_ORIGINS = {
    "background_review",
    "candidate_review",
    "curator",
    "evolve",
    "evolve_apply",
    "panel_vote",
    "probe",
    "shadow",
    "shadow_review",
    "spawned",
}


# Stable leading substring used to find running curator children in the tasks
# table for the single-flight guard. The prompt is built from this fragment so
# edits to the opening line cannot silently drift away from the detector.
CURATOR_PROMPT_PREFIX = "You are an autonomous CURATOR for thread-keeper"

CURATOR_PROMPT = CURATOR_PROMPT_PREFIX + """'s lessons + skills
library. This is a deep audit, not a filename or character-count scan. You
receive every skill ThreadKeeper tracks or materializes, including archived
records and untracked primary-store skills, plus usage telemetry.

Where the shadow_review observer LOOKS FOR new class-level learning,
your role is the inverse: review the EXISTING store for quality, dedup,
and freshness.

DEEP SKILL VALIDATOR — complete every numbered skill in order. Read the full
SKILL.md and relevant support files from `source_path`; the compact inventory
line is never sufficient. Also read AUDIT_MANIFEST_PATH, which contains
deterministic ThreadKeeper/Claude Code/Codex/Agent Skills validation, mirror
hashes, dangling links, exact-body duplicates, and lexical candidate pairs.

For EACH skill, put a numbered row in REPORT.md with:
  name | relevance/currentness | actual capability | uniqueness | consumer
  compatibility | web evidence | verdict | action/result.
Use one of these verdicts: KEEP, REPAIR, UPDATE, MERGE, SPLIT, DEPRECATE,
DELETE, CROSS_LINK, HUMAN_REVIEW. No skill may be omitted. A lexical score,
similar name, or matching character count is only a lead — decide overlap by
intent, inputs, workflow, and expected outcome after reading both full bodies.

WEB RESEARCH is mandatory for every skill (researching a coherent cluster in
one search is allowed). Prefer current official product/CLI documentation,
standards, source repositories, release notes, and then reputable public skill
catalogs. Search for comparable external skills and current alternatives. Cite
the URLs and access date in the per-skill row or its cluster note. Never copy
third-party text verbatim; use research to verify currency and identify missing
capabilities. Search queries must contain only generic capability/product terms:
never send private paths, source excerpts, secrets, user/project names, or
internal identifiers to the web. If web access fails, use HUMAN_REVIEW with
`research_unavailable` rather than claiming the skill is current.

CROSS-CLI REPAIR is mandatory when the manifest flags a supported consumer.
For a curatable skill, repair frontmatter/body/resources through skill_manage,
then call skill_validate(name=...) and require every supported consumer plus
all mirrors to pass. If post-change validation fails, restore that skill from
PASS_ID with curator_restore and record the rollback. For a protected or
external skill, do not mutate it; emit an exact HUMAN_REVIEW repair plan.

MERGE/DELETE safety:
  • Merge only when the skills have the same practical job or one fully
    subsumes the other. Preserve all unique procedures, caveats, triggers,
    examples, references, telemetry context, and useful support files in the
    umbrella skill before deleting anything.
  • DELETE only exact duplicates, fully superseded entries with no unique
    value, or demonstrably irrelevant/broken skills. Prefer CROSS_LINK or
    SPLIT when scopes are adjacent rather than identical.
  • Physical mirrors are copies of one logical skill, never duplicates.
  • Protected skills may receive a HUMAN_REVIEW recommendation but may not be
    edited/deleted automatically.
  • Write REPORT.md before mutation. A parent snapshot already exists in
    destructive mode. Revalidate every affected skill after mutation and
    record PASS/FAIL/ROLLED_BACK in REPORT.md.

OUTPUT: persist REPORT.md through
`curator_report_write(pass_id=PASS_ID, content=<full markdown>)`. Do NOT use
the filesystem Write tool for the report: sandboxed CLIs may not write outside
their project. REPORT.md is the durable human audit trail. In destructive mode,
write the complete planned report once before mutations, apply only the safe,
authorized actions below, then call curator_report_write again with actual
PASS/FAIL/ROLLED_BACK results and the final `CURATOR_PASS_COMPLETE` line.

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

LESSON RUBRIC (answer for every lesson; skills use the deep validator and
verdicts above):

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

STALE LESSONS DRY-RUN — if the inventory includes a
`## STALE LESSONS (dry-run decay ranking)` section, include a matching
section in the REPORT.md. The ranking is computed as
`access_frequency × exp(-days_since_access / tau)` and pre-filtered to
unprotected lessons with no recent access and low pull-count. This is an
advisory compost list only: do NOT call lesson_remove solely because a
lesson appears in this section. Pinned, validated, foreground, and user
entries are excluded from this list and remain off-limits.

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
    a confidence review (it may be aging out). In destructive mode you may
    apply it via concept_manage(action='set_confidence', ...); otherwise
    note it and leave it for the human.
  Note: `last_evidence_at` is a LIVE signal now — re-surfacing an
  equivalent invariant bumps it (and raises confidence), so a small
  `last_evidence` age means the concept was recently re-corroborated, not
  merely recently registered.

PROTECTION — lessons marked [PROTECTED] are pinned or foreground-authored:
only KEEP them. Protected skills are also never auto-mutated, but unlike
lessons they still require a complete audit row; use HUMAN_REVIEW with a
concrete repair/merge/deprecation recommendation when appropriate.

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


def _pass_due(conn: sqlite3.Connection, now_t: int) -> bool:
    last = _last_curator_ts(conn)
    return last <= 0 or now_t >= last + int(CURATOR_INTERVAL_S)


def _stable_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _curator_inventory_snapshot(
    conn: sqlite3.Connection,
    skill_audit: dict | None = None,
) -> dict:
    """Canonical, time-stable inventory state for debounce fingerprinting.

    Human prompt text includes relative ages and decay scores, so hashing the
    rendered dump would change as the wall clock moves. This snapshot hashes
    only stored lesson/skill/concept state that can change the curator's
    decisions.
    """
    snapshot: dict[str, list[dict]] = {
        "lessons": [],
        "skills": [],
        "skill_files": [],
        "concepts": [],
    }

    try:
        usage = lessons.lesson_usage_map(conn)
        for item in lessons.iter_lessons():
            u = usage.get(item["slug"], {})
            snapshot["lessons"].append({
                "slug": item.get("slug") or "",
                "body": item.get("body") or "",
                "ts": _stable_int(item.get("ts")),
                "source": item.get("source") or "",
                "origin": item.get("origin") or "",
                "usage": {
                    "created_at": _stable_int(u.get("created_at")),
                    "source": u.get("source") or "",
                    "last_used_at": _stable_int(u.get("last_used_at")),
                    "last_viewed_at": _stable_int(u.get("last_viewed_at")),
                    "use_count": _stable_int(u.get("use_count")) or 0,
                    "view_count": _stable_int(u.get("view_count")) or 0,
                    "pinned": _stable_int(u.get("pinned")) or 0,
                    "tier": u.get("tier") or "hypothesis",
                },
            })
    except Exception:
        logger.debug("curator: inventory lesson snapshot failed",
                     exc_info=True)

    try:
        rows = conn.execute(
            "SELECT name, created_at, created_by_origin, last_used_at, "
            "last_viewed_at, last_patched_at, use_count, view_count, "
            "patch_count, pinned, state "
            "FROM skill_usage "
            "WHERE state IN ('active', 'stale') "
            "ORDER BY name"
        ).fetchall()
        for r in rows:
            snapshot["skills"].append({
                "name": r["name"] or "",
                "created_at": _stable_int(r["created_at"]),
                "created_by_origin": r["created_by_origin"] or "",
                "last_used_at": _stable_int(r["last_used_at"]),
                "last_viewed_at": _stable_int(r["last_viewed_at"]),
                "last_patched_at": _stable_int(r["last_patched_at"]),
                "use_count": _stable_int(r["use_count"]) or 0,
                "view_count": _stable_int(r["view_count"]) or 0,
                "patch_count": _stable_int(r["patch_count"]) or 0,
                "pinned": _stable_int(r["pinned"]) or 0,
                "state": r["state"] or "",
            })
    except sqlite3.OperationalError:
        logger.debug("curator: inventory skill snapshot failed",
                     exc_info=True)

    try:
        audit = skill_audit or build_skill_audit(conn, include_archived=True)
        snapshot["skill_files"] = [
            {
                "name": record["name"],
                "state": record["state"],
                "source_path": record["source_path"],
                "content_sha256": record["content_sha256"],
                "normalized_body_sha256": record["normalized_body_sha256"],
                "mirrors": record["mirrors"],
                "findings": record["findings"],
            }
            for record in audit["skills"]
        ]
    except Exception:
        logger.debug("curator: deep skill snapshot failed", exc_info=True)

    try:
        rows = conn.execute(
            "SELECT id, description, confidence, registered_at, "
            "last_evidence_at FROM concepts ORDER BY id"
        ).fetchall()
        for r in rows:
            snapshot["concepts"].append({
                "id": r["id"] or "",
                "description": r["description"] or "",
                "confidence": r["confidence"] or "",
                "registered_at": _stable_int(r["registered_at"]),
                "last_evidence_at": _stable_int(r["last_evidence_at"]),
            })
    except sqlite3.OperationalError:
        pass

    snapshot["lessons"].sort(key=lambda row: row["slug"])
    return snapshot


def _inventory_fingerprint(snapshot: dict) -> str:
    payload = json.dumps(
        {"version": 1, "inventory": snapshot},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _current_inventory_fingerprint(
    conn: sqlite3.Connection,
    skill_audit: dict | None = None,
) -> tuple[str, int, int, int]:
    snapshot = _curator_inventory_snapshot(conn, skill_audit=skill_audit)
    return (
        _inventory_fingerprint(snapshot),
        len(snapshot["lessons"]),
        len(snapshot["skill_files"]) or len(snapshot["skills"]),
        len(snapshot["concepts"]),
    )


def _last_inventory_fingerprint(
    conn: sqlite3.Connection,
) -> tuple[str | None, int | None]:
    """Latest completed/endorsed curator inventory fingerprint.

    Stored in the existing `curator_pass` summary so no schema migration is
    required. Rows without the key are from older versions or non-inventory
    outcomes such as below-threshold / spawn-error.
    """
    try:
        rows = conn.execute(
            "SELECT target, summary, created_at FROM events "
            "WHERE kind='curator_pass' ORDER BY id DESC LIMIT 50"
        ).fetchall()
    except sqlite3.OperationalError:
        return None, None
    for r in rows:
        summary = r["summary"] or ""
        match = _INVENTORY_FINGERPRINT_RE.search(summary)
        if not match:
            continue
        ts = _stable_int(r["target"]) or _stable_int(r["created_at"])
        return match.group(1), ts
    return None, None


def _format_lesson(item: dict, usage: dict | None = None) -> str:
    """One inventory line per lesson.

    Foreground/user lessons, pinned lesson_usage rows, and validated
    lesson_usage rows get the PROTECTED marker so the curator never proposes
    destructive changes against them."""
    usage = usage or {}
    src = (usage.get("source") or item.get("source") or "").strip()
    is_protected, _reason = lessons.lesson_protection(item, usage)
    protected = " [PROTECTED]" if is_protected else ""
    ts = item.get("ts") or 0
    now_t = int(time.time())
    age_d = (now_t - ts) // 86400 if ts else "?"
    last_active = max(
        usage.get("last_used_at") or 0,
        usage.get("last_viewed_at") or 0,
        ts or 0,
    )
    last_active_d = (now_t - last_active) // 86400 if last_active else "?"
    body_preview = (item.get("body") or "")[:200].replace("\n", " ")
    if len(item.get("body") or "") > 200:
        body_preview += "…"
    return (
        f"- LESSON {item['slug']}{protected} "
        f"(source={src or '?'}, tier={usage.get('tier') or 'hypothesis'}, "
        f"uses={usage.get('use_count', 0)}, views={usage.get('view_count', 0)}, "
        f"pinned={usage.get('pinned', 0)}, age={age_d}d, "
        f"last_active={last_active_d}d_ago)\n"
        f"    body: {body_preview}"
    )


def _format_skill(row: dict) -> str:
    """One inventory line per recently-touched skill row from
    skill_usage. Foreground/unknown-origin and pinned skills are PROTECTED."""
    origin = row.get("created_by_origin") or "?"
    protected = ""
    if (
        row.get("pinned")
        or origin == "foreground"
        or origin == "?"
        or origin not in _CURATABLE_SKILL_ORIGINS
    ):
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


def _collect_stale_lessons(conn: sqlite3.Connection) -> tuple[str, int]:
    """Build the advisory stale-lessons decay section.

    This is intentionally a dry-run list. It gives the human/curator a ranked
    compost candidate set, but the score by itself is not a deletion command.
    """
    try:
        rows = lessons.rank_stale_lessons(conn)
    except Exception:
        logger.debug("curator: rank_stale_lessons failed", exc_info=True)
        rows = []
    lines = [
        "## STALE LESSONS (dry-run decay ranking)\n",
        "Advisory only; never auto-delete solely from this list.",
    ]
    if not rows:
        lines.append("(none)")
        return "\n".join(lines), 0
    for r in rows:
        age = int(r["age_days"])
        lines.append(
            f"- {r['slug']} score={r['decay_score']:.6f} "
            f"freq={r['access_frequency']:.4f}/d "
            f"pulls={r['pull_count']} uses={r['use_count']} "
            f"views={r['view_count']} last_access={age}d_ago "
            f"tier={r['tier']} pinned={r['pinned']} "
            f"source={r['source'] or '?'}"
        )
    return "\n".join(lines), len(rows)


def _collect_inventory(
    conn: sqlite3.Connection,
    skill_audit: dict | None = None,
) -> tuple[str, int, int]:
    """Build the inventory dump the curator child will read.

    Returns (dump_text, lesson_count, skill_count). The dump format is
    plain text — `_format_lesson` and `_format_skill` produce one line
    per entry, grouped into LESSONS and SKILLS sections.
    """
    # ---- Lessons ----
    lesson_lines: list[str] = []
    n_lessons = 0
    try:
        usage = lessons.lesson_usage_map(conn)
        for item in lessons.iter_lessons():
            lesson_lines.append(_format_lesson(item, usage.get(item["slug"])))
            n_lessons += 1
    except Exception:
        logger.debug("curator: iter_lessons failed", exc_info=True)

    # ---- Skills ----
    audit = skill_audit or build_skill_audit(conn, include_archived=True)
    skill_checklist = format_skill_checklist(audit)
    n_skills = int(audit["summary"]["total"])

    parts: list[str] = []
    parts.append(f"## LESSONS (n={n_lessons})\n")
    parts.extend(lesson_lines if lesson_lines else ["(none)"])
    stale_text, _n_stale = _collect_stale_lessons(conn)
    parts.append("\n" + stale_text)
    parts.append("\n" + skill_checklist)

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
# Single-flight: one curator pass at a time across ALL processes
# ──────────────────────────────────────────────────────────────────────

def _running_curator_children(conn: sqlite3.Connection) -> list[str]:
    """Running curator task ids, reaping dead rows.

    The curator mutates ONE shared store (lessons.md + skill files). Two
    curators launched from different foreground MCP servers — or a daemon tick
    racing a manual curator_run — read the same inventory and, in destructive
    mode, apply overlapping PRUNE/CONSOLIDATE edits that double-apply or clobber
    each other. So the loop is machine-wide single-flight.
    """
    from .helpers import alive
    try:
        rows = conn.execute(
            "SELECT id, pid FROM tasks WHERE ended_at IS NULL "
            "AND prompt LIKE ?",
            (CURATOR_PROMPT_PREFIX + "%",),
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


def _curator_spawn_lock():
    """Cross-process guard for check-running-then-spawn.

    The tasks-table running check is necessary but NOT atomic across foreground
    MCP processes — between the SELECT and the spawn there is a TOCTOU window in
    which two ticks both observe no curator and both spawn. A non-blocking
    flock closes it: only one process holds the lock, the rest skip the pass.
    Manual curator_run(force=True) bypasses the interval but still respects this
    lock.
    """
    return single_flight_lock("curator")


# ──────────────────────────────────────────────────────────────────────
# Synchronous pass + daemon loop
# ──────────────────────────────────────────────────────────────────────

def run_curator_pass(force: bool = False, *, scheduled: bool = False) -> str:
    """Execute one curator pass synchronously. Used by the daemon AND
    by the MCP tool for manual triggering / testing.

    Returns a short status string for observability:
      - 'disabled'        — env knob off and not forced
      - 'not_due'         — checked recently; interval high-water not due
      - 'curator_running n=…' — a curator child is already running; skip
      - 'below_threshold' — fewer than CURATOR_MIN_LESSONS lessons; skip
      - 'unchanged_inventory' — latest complete inventory already reviewed
      - 'spawned task_id=…' — curator child launched
      - 'spawn_error: …'  — spawn() rejected
    """
    if CURATOR_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    now = int(time.time())
    if not force and not _pass_due(conn, now):
        _record_curator_pass(conn, _last_curator_ts(conn), "not_due")
        return "not_due"
    if not daemon_state.claim_pass(
        "curator", CURATOR_INTERVAL_S, scheduled=scheduled, conn=conn,
        now=now,
    ):
        _record_curator_pass(conn, _last_curator_ts(conn), "not_due")
        return "not_due"

    # Single-flight: the flock makes the running-children check + spawn atomic
    # across every MCP server process, so two ticks (or a tick racing a manual
    # curator_run) can't both spawn against the same shared store. force=True
    # bypasses the interval, never the lock.
    with _curator_spawn_lock() as locked:
        if not locked:
            return "curator_running n=1 (single-flight lock)"

        running = _running_curator_children(conn)
        if running:
            out = f"curator_running n={len(running)} (single-flight)"
            _record_curator_pass(conn, now, out)
            return out

        try:
            skill_audit = build_skill_audit(conn, include_archived=True)
        except Exception as exc:
            out = f"skill_audit_error: {exc}"
            _record_curator_pass(conn, now, out)
            return out
        fingerprint, n_lessons, n_skills, n_concepts = (
            _current_inventory_fingerprint(conn, skill_audit=skill_audit)
        )
        if n_lessons < CURATOR_MIN_LESSONS and n_skills == 0:
            _record_curator_pass(
                conn, now,
                f"below_threshold lessons={n_lessons} skills={n_skills}",
            )
            return f"below_threshold lessons={n_lessons}"

        last_fingerprint, last_fingerprint_ts = _last_inventory_fingerprint(
            conn
        )
        # Scheduled passes deliberately re-run unchanged content: relevance,
        # CLI behavior, and external alternatives can change even when local
        # bytes do not. Manual duplicate calls still debounce for safety/cost.
        if last_fingerprint == fingerprint and not scheduled:
            ts_part = (
                f" endorsed_ts={last_fingerprint_ts}"
                if last_fingerprint_ts else ""
            )
            outcome = (
                f"unchanged_inventory {INVENTORY_FINGERPRINT_KEY}="
                f"{fingerprint}{ts_part} lessons={n_lessons} "
                f"skills={n_skills} concepts={n_concepts}"
            )
            _record_curator_pass(conn, now, outcome)
            return (
                "unchanged_inventory "
                f"fingerprint={fingerprint[:12]}{ts_part}"
            )

        inventory, _n_lessons, _n_skills = _collect_inventory(
            conn, skill_audit=skill_audit,
        )

        # Concepts enrich the review but do NOT lower the lesson threshold —
        # a curator pass is only worth a child spawn when there's a real
        # lesson/skill inventory to audit; concepts ride along.
        concepts_text, _n_concepts = _collect_concepts(conn)
        if concepts_text:
            inventory = inventory + "\n\n" + concepts_text

        # Ensure reports dir exists before the child tries to Write into it.
        CURATOR_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        pass_id = time.strftime('%Y%m%dT%H%M%S')
        manifest_path = write_skill_audit_manifest(
            skill_audit,
            CURATOR_REPORTS_DIR / f"AUDIT-{pass_id}.json",
        )
        snapshot_dir = None

        if CURATOR_DESTRUCTIVE:
            try:
                snapshot_dir = create_curator_snapshot(
                    pass_id,
                    conn=conn,
                    retention=CURATOR_SNAPSHOT_RETENTION,
                )
            except Exception as e:
                out = f"snapshot_error: {e}"
                _record_curator_pass(conn, now, out)
                return out

        # Default: destructive — the curator applies its own recommendations
        # after writing the REPORT. THREADKEEPER_CURATOR_DESTRUCTIVE=0 reverts
        # to advisory REPORT-only (read-only toolset).
        if CURATOR_DESTRUCTIVE:
            foreground_clause = (
                "This pass has explicit, snapshot-scoped authority to mutate "
                "foreground-authored skills when the manifest does not mark "
                "them protected. Pinned and untracked skills remain protected."
                if CURATOR_MANAGE_FOREGROUND_SKILLS else
                "Foreground-authored, pinned, and untracked skills remain "
                "protected and require HUMAN_REVIEW."
            )
            destructive_clause = (
                "DESTRUCTIVE MODE ENABLED (this is the default). After writing "
                "the REPORT.md you MUST apply your own PATCH / PRUNE / "
                "CONSOLIDATE recommendations directly:\n"
                "  • PATCH — lesson_append(...) replaces a same-slug lesson in "
                "place; skill_manage(action='patch') for skills.\n"
                "  • PRUNE — lesson_remove(slug=...) for a lesson; "
                "skill_manage(action='delete') for a skill.\n"
                "  • CONSOLIDATE — write the umbrella entry first, then "
                "lesson_remove / skill_manage(action='delete') each merged-away "
                "slug so the duplicate copies are actually gone.\n"
                "  • CONSOLIDATE_CONCEPT / PRUNE_CONCEPT — apply concept "
                "recommendations directly: concept_manage(action='consolidate', "
                "concept_id=<kept-id>, merge_ids='<id-a>,<id-b>') folds the "
                "duplicates into the kept concept and deletes them; "
                "concept_manage(action='remove', concept_id=<id>) prunes a "
                "false-positive concept; concept_manage(action='set_confidence', "
                "concept_id=<id>, confidence='low|medium|high') applies a "
                "confidence review.\n"
                "NEVER pass force=True to lesson_remove or skill_manage. "
                f"{foreground_clause} NEVER touch "
                "any entry marked [PROTECTED], even in destructive mode. Apply "
                "changes ONLY after the REPORT.md is "
                "written (audit trail first, mutation second). A recovery "
                f"snapshot for this pass already exists at {snapshot_dir}."
            )
            allowed_tools = (
                "mcp__thread-keeper__lesson_list,"
                "mcp__thread-keeper__lesson_get,"
                "mcp__thread-keeper__lesson_append,"
                "mcp__thread-keeper__lesson_remove,"
                "mcp__thread-keeper__skill_list,"
                "mcp__thread-keeper__skill_manage,"
                "mcp__thread-keeper__skill_validate,"
                "mcp__thread-keeper__curator_report_write,"
                "mcp__thread-keeper__curator_restore,"
                "mcp__thread-keeper__list_concepts,"
                "mcp__thread-keeper__expand_concept,"
                "mcp__thread-keeper__concept_manage,"
                "mcp__thread-keeper__evolve_format,"
                "Read,WebSearch,WebFetch"
            )
        else:
            destructive_clause = (
                "ADVISORY MODE (you explicitly set "
                "THREADKEEPER_CURATOR_DESTRUCTIVE=0). Do NOT call lesson_append, "
                "lesson_remove, skill_manage with action in "
                "{create,patch,delete,write_file}, or any other destructive tool. "
                "Your output is the REPORT.md ONLY — the human reviews and applies "
                "changes manually. Unset the knob (or set it to 1) to let the "
                "curator apply its own recommendations directly, the default."
            )
            allowed_tools = (
                "mcp__thread-keeper__lesson_list,"
                "mcp__thread-keeper__lesson_get,"
                "mcp__thread-keeper__skill_list,"
                "mcp__thread-keeper__skill_validate,"
                "mcp__thread-keeper__curator_report_write,"
                "mcp__thread-keeper__list_concepts,"
                "mcp__thread-keeper__expand_concept,"
                "mcp__thread-keeper__evolve_format,"
                "Read,WebSearch,WebFetch"
            )

        full_prompt = (
            CURATOR_PROMPT.replace("{DESTRUCTIVE_CLAUSE}", destructive_clause)
            + inventory
            + "\n\n"
            + f"REPORT_PATH = {CURATOR_REPORTS_DIR}/REPORT-"
            f"{pass_id}.md\n"
            + f"AUDIT_MANIFEST_PATH = {manifest_path}\n"
            + f"PASS_ID = {pass_id}\n"
            + "Persist the REPORT through curator_report_write using PASS_ID; "
            + "REPORT_PATH is informational and must not be written directly."
        )

        from .tools.spawn import spawn  # type: ignore
        old_pass = os.environ.get(PASS_ID_ENV)
        old_snap = os.environ.get(SNAPSHOT_DIR_ENV)
        if CURATOR_DESTRUCTIVE:
            os.environ[PASS_ID_ENV] = pass_id
            os.environ[SNAPSHOT_DIR_ENV] = str(snapshot_dir)
        try:
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
            finally:
                if old_pass is None:
                    os.environ.pop(PASS_ID_ENV, None)
                else:
                    os.environ[PASS_ID_ENV] = old_pass
                if old_snap is None:
                    os.environ.pop(SNAPSHOT_DIR_ENV, None)
                else:
                    os.environ[SNAPSHOT_DIR_ENV] = old_snap
        except Exception as e:
            _record_curator_pass(conn, now, f"spawn_error: {e}")
            return f"spawn_error: {e}"

        _record_curator_pass(
            conn, now,
            f"spawned {INVENTORY_FINGERPRINT_KEY}={fingerprint} "
            f"lessons={n_lessons} skills={n_skills} "
            f"concepts={n_concepts} "
            f"manifest={manifest_path.name} "
            f"snapshot={pass_id if snapshot_dir else '-'} "
            f":: {str(result)[:140]}",
        )
        return str(result)


def _serve_loop() -> None:
    """Daemon body. Sleep → tick → sleep, until process dies."""
    while True:
        try:
            run_curator_pass(scheduled=True)
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
