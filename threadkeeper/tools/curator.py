"""MCP tools for the Curator (lessons + skills library audit).

The age-based archival pass `curator_run` lives in tools.skills (it
mutates skill_usage state). These tools are about the *LLM-driven*
audit pass:

  curator_review(force=False, dry_run=False)
    Trigger one curator pass NOW. Spawns a slim child with the inventory
    of every lesson + skill, child writes a REPORT.md with KEEP / PATCH
    / CONSOLIDATE / PRUNE recommendations.

  curator_review_status()
    Diagnostic: env config, last cursor, last 5 passes, latest REPORT
    path, latest destructive snapshot path.

  curator_restore(pass_id, lesson_slug="", skill_name="")
    Restore one lesson or skill from a destructive pass snapshot.
"""

from __future__ import annotations

import time

from .._mcp import read_tool, write_tool
from ..db import get_db
from ..identity import _ensure_session
from ..curator import (
    CURATOR_PROMPT,
    _collect_inventory,
    _last_curator_ts,
    run_curator_pass,
)
from ..curator_snapshots import restore_lesson, restore_skill, snapshots_root
from ..config import (
    CURATOR_INTERVAL_S,
    CURATOR_MIN_LESSONS,
    CURATOR_REPORTS_DIR,
    CURATOR_DESTRUCTIVE,
)


@write_tool()
def curator_review(force: bool = False, dry_run: bool = False) -> str:
    """Fire one curator pass.

    `force=True` runs even when CURATOR_INTERVAL_S=0 (daemon disabled).
    Use for one-shot triage or testing the prompt.

    `dry_run=True` short-circuits before the spawn — returns the
    inventory that WOULD be passed plus n_lessons/n_skills. No spawn,
    no cursor advance. Use to inspect what the curator child would see
    before paying for the spawn.
    """
    conn = get_db()
    _ensure_session(conn)
    if dry_run:
        inventory, n_lessons, n_skills = _collect_inventory(conn)
        below = n_lessons < CURATOR_MIN_LESSONS
        head = inventory[:2000]
        suffix = "…(truncated for display)" if len(inventory) > 2000 else ""
        return (
            f"dry_run: lessons={n_lessons} skills={n_skills} "
            f"min_lessons={CURATOR_MIN_LESSONS} "
            f"would_spawn={'no (below_threshold)' if below else 'yes'}\n\n"
            f"--- prompt preview ---\n"
            f"{CURATOR_PROMPT[:400]}…\n\n"
            f"--- inventory head ---\n{head}{suffix}"
        )
    return run_curator_pass(force=force)


@read_tool()
def curator_review_status() -> str:
    """Show curator configuration + last 5 passes + latest REPORT path.

    Sanity-check for whether the daemon is alive, advancing the cursor,
    and producing REPORTs the user can read."""
    conn = get_db()
    _ensure_session(conn)
    floor = _last_curator_ts(conn)
    now = int(time.time())
    age_s = (now - floor) if floor else None
    mode = "destructive" if CURATOR_DESTRUCTIVE else "advisory"
    lines = [
        f"interval_s={CURATOR_INTERVAL_S:.0f} "
        f"min_lessons={CURATOR_MIN_LESSONS} "
        f"mode={mode} "
        f"reports_dir={CURATOR_REPORTS_DIR}",
        f"cursor_ts={floor} (age={age_s}s)" if floor
        else "cursor_ts=0 (no prior pass)",
        "",
        "recent passes (newest first):",
    ]
    try:
        rows = conn.execute(
            "SELECT created_at, summary FROM events "
            "WHERE kind='curator_pass' "
            "ORDER BY id DESC LIMIT 5"
        ).fetchall()
    except Exception:
        rows = []
    if not rows:
        lines.append("  (none)")
    else:
        for r in rows:
            ts = r["created_at"]
            age = now - int(ts) if ts else 0
            snip = (r["summary"] or "")[:120]
            lines.append(f"  {age}s_ago  {snip}")

    # Latest REPORT.md the curator wrote, if any.
    try:
        reports = sorted(
            CURATOR_REPORTS_DIR.glob("REPORT-*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except FileNotFoundError:
        reports = []
    lines.append("")
    if reports:
        lines.append(f"latest_report={reports[0]}")
    else:
        lines.append("latest_report=(none yet)")
    root = snapshots_root(CURATOR_REPORTS_DIR)
    try:
        snaps = sorted(
            [p for p in root.iterdir() if p.is_dir()],
            key=lambda p: p.name,
            reverse=True,
        )
    except FileNotFoundError:
        snaps = []
    if snaps:
        lines.append(f"latest_snapshot={snaps[0]}")
    else:
        lines.append("latest_snapshot=(none yet)")
    return "\n".join(lines)


@write_tool(destructive=True, idempotent=True)
def curator_restore(
    pass_id: str,
    lesson_slug: str = "",
    skill_name: str = "",
) -> str:
    """Restore one lesson or skill from a curator pre-mutation snapshot.

    Pass exactly one of `lesson_slug` or `skill_name`. Restoring a lesson
    replaces the current same-slug section if present, otherwise re-adds it.
    Restoring a skill replaces the primary skill dir and mirrors it to the
    configured skill roots.
    """
    conn = get_db()
    _ensure_session(conn)
    lesson_slug = lesson_slug.strip()
    skill_name = skill_name.strip()
    if bool(lesson_slug) == bool(skill_name):
        return "ERR pass exactly one of lesson_slug or skill_name"
    if lesson_slug:
        return restore_lesson(pass_id, lesson_slug, conn)
    return restore_skill(pass_id, skill_name, conn)
