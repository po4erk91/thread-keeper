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
    path, inventory fingerprints, and latest destructive snapshot path.

  curator_restore(pass_id, lesson_slug="", skill_name="")
    Restore one lesson or skill from a destructive pass snapshot.
"""

from __future__ import annotations

import json
import os
import re
import time

from .._mcp import read_tool, write_tool
from ..db import get_db
from ..identity import _ensure_session
from ..curator import (
    CURATOR_PROMPT,
    _collect_inventory,
    _current_inventory_fingerprint,
    _last_inventory_fingerprint,
    _last_curator_ts,
    run_curator_pass,
)
from ..curator_snapshots import restore_lesson, restore_skill, snapshots_root
from ..skill_audit import build_skill_audit
from ..config import (
    CURATOR_INTERVAL_S,
    CURATOR_MIN_LESSONS,
    CURATOR_REPORTS_DIR,
    CURATOR_DESTRUCTIVE,
    CURATOR_MANAGE_FOREGROUND_SKILLS,
)


_PASS_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_MAX_REPORT_CHARS = 2_000_000


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
        audit = build_skill_audit(conn, include_archived=True)
        inventory, n_lessons, n_skills = _collect_inventory(
            conn, skill_audit=audit,
        )
        below = n_lessons < CURATOR_MIN_LESSONS and n_skills == 0
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
    """Show curator config + inventory fingerprints + latest REPORT path.

    Sanity-check for whether the daemon is alive, advancing the cursor,
    and producing REPORTs the user can read."""
    conn = get_db()
    _ensure_session(conn)
    floor = _last_curator_ts(conn)
    now = int(time.time())
    age_s = (now - floor) if floor else None
    last_fingerprint, fingerprint_ts = _last_inventory_fingerprint(conn)
    fingerprint_age_s = (now - fingerprint_ts) if fingerprint_ts else None
    mode = "destructive" if CURATOR_DESTRUCTIVE else "advisory"
    lines = [
        f"interval_s={CURATOR_INTERVAL_S:.0f} "
        f"min_lessons={CURATOR_MIN_LESSONS} "
        f"mode={mode} "
        f"manage_foreground_skills={int(CURATOR_MANAGE_FOREGROUND_SKILLS)} "
        f"reports_dir={CURATOR_REPORTS_DIR}",
        f"cursor_ts={floor} (age={age_s}s)" if floor
        else "cursor_ts=0 (no prior pass)",
    ]
    if last_fingerprint:
        lines.append(
            f"inventory_sha256={last_fingerprint} "
            f"(age={fingerprint_age_s}s)"
        )
    else:
        lines.append("inventory_sha256=(none)")
    try:
        current_fp, n_lessons, n_skills, n_concepts = (
            _current_inventory_fingerprint(conn)
        )
        lines.append(
            f"current_inventory_sha256={current_fp} lessons={n_lessons} "
            f"skills={n_skills} concepts={n_concepts}"
        )
    except Exception:
        lines.append("current_inventory_sha256=(unavailable)")
    lines.extend(["", "recent passes (newest first):"])
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
    try:
        manifests = sorted(
            CURATOR_REPORTS_DIR.glob("AUDIT-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except FileNotFoundError:
        manifests = []
    lines.append(
        f"latest_audit_manifest={manifests[0]}"
        if manifests else "latest_audit_manifest=(none yet)"
    )
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


@read_tool()
def skill_validate(name: str = "", include_archived: bool = True) -> str:
    """Validate ThreadKeeper-managed skills across supported CLI consumers.

    With `name`, returns the complete deterministic record for one logical
    skill, including ThreadKeeper/Claude Code/Codex/Agent Skills validation,
    mirror hashes, local-link findings, exact duplicates, and semantic
    candidates involving that skill. Curator must call this after every skill
    mutation. Without `name`, returns a compact complete numbered inventory.
    Semantic candidates are leads, never automatic delete decisions.
    """
    conn = get_db()
    _ensure_session(conn)
    manifest = build_skill_audit(conn, include_archived=include_archived)
    requested = name.strip()
    if requested:
        record = next(
            (item for item in manifest["skills"] if item["name"] == requested),
            None,
        )
        if record is None:
            return f"ERR skill_not_found={requested}"
        exact = [
            group for group in manifest["exact_duplicate_groups"]
            if requested in group["skills"]
        ]
        candidates = [
            pair for pair in manifest["semantic_candidates"]
            if requested in {pair["left"], pair["right"]}
        ]
        return json.dumps(
            {
                "schema_version": manifest["schema_version"],
                "skill": record,
                "exact_duplicate_groups": exact,
                "semantic_candidates": candidates,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    rows = []
    for index, record in enumerate(manifest["skills"], start=1):
        rows.append({
            "index": index,
            "name": record["name"],
            "state": record["state"],
            "protected": record["protected"],
            "source_path": record["source_path"],
            "validators": record["validators"],
            "mirrors": record["mirrors"],
            "findings": record["findings"],
        })
    return json.dumps(
        {
            "schema_version": manifest["schema_version"],
            "summary": manifest["summary"],
            "skills": rows,
            "exact_duplicate_groups": manifest["exact_duplicate_groups"],
            "semantic_candidates": manifest["semantic_candidates"],
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@write_tool(idempotent=True)
def curator_report_write(pass_id: str, content: str) -> str:
    """Atomically write one Curator report inside the configured report dir.

    This narrow tool is the cross-CLI alternative to direct filesystem Write:
    Codex workspace sandboxes cannot write ``~/.threadkeeper/curator`` when the
    project is elsewhere. ``pass_id`` is filename-safe and cannot select an
    arbitrary path. Repeated calls replace the same pass report so the Curator
    can persist its plan before mutation and then add actual validation/
    rollback results afterward.
    """
    clean_id = pass_id.strip()
    if not clean_id or not _PASS_ID_RE.fullmatch(clean_id):
        return "ERR invalid_pass_id"
    if len(content) > _MAX_REPORT_CHARS:
        return f"ERR report_too_large max_chars={_MAX_REPORT_CHARS}"
    if not content.strip():
        return "ERR empty_report"
    CURATOR_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    target = CURATOR_REPORTS_DIR / f"REPORT-{clean_id}.md"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        temporary.replace(target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return f"ERR report_write_failed={exc}"
    return f"ok path={target} chars={len(content)}"


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
