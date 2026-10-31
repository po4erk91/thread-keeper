"""MCP tools for the Curator (lessons + skills library audit).

The age-based archival pass `curator_run` lives in tools.skills (it
mutates skill_usage state). These tools are about the *LLM-driven*
audit pass:

  curator_review(force=False, dry_run=False)
    Trigger one curator pass NOW. Spawns bounded slim-child batches over
    the lesson + skill + concept inventory; each child writes a REPORT.md
    with KEEP / PATCH / CONSOLIDATE / PRUNE recommendations.

  curator_review_status()
    Diagnostic: env config, last cursor, last 5 passes, latest REPORT
    path, inventory fingerprints, and latest destructive snapshot path.

  curator_restore(pass_id, lesson_slug="", skill_name="")
    Restore one lesson or skill from a destructive pass snapshot.

  curator_research_write(pass_id, content, batch_index, batch_total)
    Persist one parent-authorized, bounded public-research handoff. Only the
    web-only Curator research phase can call it.

  curator_merge_verdict(slug_a, slug_b, reason)
    Remember a reviewed lesson pair that must remain separate.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from .._mcp import read_tool, write_tool
from ..db import get_db
from ..identity import _detect_self_cid, _ensure_session
from ..curator import (
    CURATOR_RESEARCH_MAX_CHARS,
    CURATOR_RESEARCH_PROVENANCE_KIND,
    CURATOR_PROMPT,
    CURATOR_REPORT_PROVENANCE_KIND,
    _collect_inventory,
    _curator_researcher_owns_launch,
    _curator_writer_owns_launch,
    _current_inventory_fingerprint,
    _last_inventory_fingerprint,
    _last_curator_ts,
    _record_batch_report_provenance,
    curator_research_authorization,
    curator_research_payload,
    curator_report_sha256,
    lesson_promotion_telemetry,
    curator_pass_status,
    CURATOR_REPORT_COMPLETE_MARKER,
    curator_report_name,
    record_merge_verdict,
    run_curator_pass,
)
from ..curator_snapshots import (
    EVALUATE_PHASE,
    PHASE_ENV,
    VERIFY_PHASE,
    current_pass_id,
    verifier_scope_refusal,
    restore_lesson,
    restore_skill,
    snapshots_root,
)
from ..permissions import chmod_private_file
from ..link_health import scan_wikilink_health
from ..skill_audit import build_skill_audit, reconcile_source_availability
from ..config import (
    CURATOR_INTERVAL_S,
    CURATOR_MIN_LESSONS,
    CURATOR_REPORTS_DIR,
    CURATOR_DESTRUCTIVE,
    CURATOR_MAX_CONCURRENT_BATCHES,
    CURATOR_MANAGE_FOREGROUND_SKILLS,
    SPAWNED_CHILD,
    WRITE_ORIGIN,
)


_PASS_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_MAX_REPORT_CHARS = 2_000_000
logger = logging.getLogger(__name__)


@write_tool(idempotent=True)
def curator_verify_lesson(
    slug: str,
    lesson_revision: str,
    claim: str,
    status: str,
    source_url: str,
    source_revision: str = "",
    source_text: str = "",
) -> str:
    """Record a protected lesson's factual freshness without editing its text.

    Read lesson_verifications for the lesson revision. Check the exact claim
    against an official HTTPS page. Pass its official revision/ETag as
    source_revision, or its checked text as source_text for server-side hashing.
    Status: verified, contradicted, or unsubstantiated.
    The server stamps check time. Reaudits replace the same claim record;
    a changed source invalidates other results based on its older revision.
    This tool records supplied evidence; it does not fetch or certify a URL.
    """
    from ..lesson_freshness import checked_source_revision, record_verification
    from ..lessons import _lessons_file_lock, get_path, iter_lessons, lesson_protection

    conn = get_db()
    _ensure_session(conn)
    with _lessons_file_lock(get_path()):
        item = next((item for item in iter_lessons() if item["slug"] == slug), None)
        if item is None:
            return f"ERR not_found slug={slug}"
        usage = conn.execute("SELECT * FROM lesson_usage WHERE slug=?", (slug,)).fetchone()
        if not lesson_protection(item, dict(usage) if usage else None)[0]:
            return f"ERR lesson_not_protected slug={slug}"
        try:
            revision = checked_source_revision(source_revision, source_text)
            result = record_verification(
                conn, item, lesson_revision, claim, status, source_url, revision,
            )
        except ValueError as exc:
            return f"ERR {exc}"
        conn.commit()
    return (
        f"ok slug={slug} status={status} claim_fingerprint={result['claim_fingerprint']} "
        f"checked_at={result['checked_at']}"
    )


@write_tool(idempotent=True)
def curator_merge_verdict(slug_a: str, slug_b: str, reason: str) -> str:
    """Remember a curator-reviewed lesson pair that should remain separate.

    This records only the safe `keep_both` outcome for a rejected merge
    candidate. The next curator inventory includes the pair and short reason,
    so a later pass can respect the deliberate layering without re-reading
    both full lesson bodies.
    """
    conn = get_db()
    _ensure_session(conn)
    try:
        left, right = record_merge_verdict(conn, slug_a, slug_b, reason)
    except ValueError as exc:
        return f"ERR {exc}"
    return f"ok merge_verdict={left},{right} decision=keep_both"


@write_tool()
def curator_review(force: bool = False, dry_run: bool = False) -> str:
    """Fire one curator pass.

    `force=True` runs even when CURATOR_INTERVAL_S=0 (daemon disabled).
    Use for one-shot triage or testing the prompt.

    `dry_run=True` short-circuits before the spawn — returns the
    capped inventory preview plus n_lessons/n_skills. No spawn, no cursor
    advance. Use to inspect the inventory shape before paying for the
    batched spawn.
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
        f"max_concurrent_batches={CURATOR_MAX_CONCURRENT_BATCHES} "
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
    batch_state = curator_pass_status(conn)
    lines.append(
        "batch_state "
        f"pass_id={batch_state['pass_id'] or '-'} "
        f"expected={batch_state['expected']} "
        f"running={batch_state['running']} "
        f"failed={batch_state['failed']} "
        f"complete={batch_state['complete']} "
        f"unapplied={batch_state['unapplied']} "
        f"endorsed={int(bool(batch_state['endorsed']))}"
    )
    try:
        collection, current_fp, n_lessons, n_skills, n_concepts = (
            _current_inventory_fingerprint(conn)
        )
        if collection.completeness.complete:
            lines.append(
                f"current_inventory_sha256={current_fp} lessons={n_lessons} "
                f"skills={n_skills} concepts={n_concepts}"
            )
        else:
            lines.append(
                "current_inventory_sha256=(unavailable) "
                + collection.completeness.failure_outcome()
            )
    except Exception:
        lines.append("current_inventory_sha256=(unavailable)")
    lines.append(lesson_promotion_telemetry(conn))
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


@write_tool(idempotent=True)
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
    reconciled = reconcile_source_availability(conn, manifest["skills"])
    if reconciled:
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
                "reconciled": requested in reconciled,
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
            "availability": record["availability"],
            "compatible": record["compatible"],
            "owner_action": record["owner_action"],
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
            "reconciled": reconciled,
            "skills": rows,
            "exact_duplicate_groups": manifest["exact_duplicate_groups"],
            "semantic_candidates": manifest["semantic_candidates"],
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@read_tool()
def wikilink_health(include_archived: bool = True) -> str:
    """List unresolved ``[[slug]]`` links in all lesson and skill bodies.

    This detector is read-only. It reports each dead target with the lesson
    or skill that references it; repair remains a separate curator action.
    """
    conn = get_db()
    _ensure_session(conn)
    return json.dumps(
        scan_wikilink_health(conn, include_archived=include_archived),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


@write_tool(idempotent=True)
def curator_research_write(
    pass_id: str = "",
    content: str = "",
    batch_index: int = 0,
    batch_total: int = 0,
) -> str:
    """Persist bounded public web evidence to a parent-authorized handoff.

    Only a spawned ``curator_researcher`` child carrying the matching pass ID may
    call this tool. The parent chooses the filename from pass and batch metadata;
    callers cannot select a destination or hand evidence to another batch. The
    web-only child receives no local inventory, memory, or repository access.
    """
    supplied_id = pass_id.strip()
    if supplied_id and not _PASS_ID_RE.fullmatch(supplied_id):
        return "ERR invalid_pass_id"
    try:
        batch_index = int(batch_index)
        batch_total = int(batch_total)
    except (TypeError, ValueError):
        return "ERR invalid_batch"
    if not (1 <= batch_index <= batch_total <= 9999):
        return "ERR invalid_batch"
    evidence = content.rstrip()
    if len(evidence) > CURATOR_RESEARCH_MAX_CHARS:
        return f"ERR research_too_large max_chars={CURATOR_RESEARCH_MAX_CHARS}"
    if not evidence or not evidence.endswith("CURATOR_RESEARCH_COMPLETE"):
        return "ERR malformed_research"
    if WRITE_ORIGIN != "curator_research" or not SPAWNED_CHILD:
        return "ERR research_write_not_authorized"
    clean_id = current_pass_id()
    if not clean_id:
        return "ERR research_write_pass_mismatch"
    if supplied_id and supplied_id != clean_id:
        return "ERR research_write_pass_mismatch"
    conn = get_db()
    _ensure_session(conn)
    authorization = curator_research_authorization(
        conn, clean_id, batch_index, batch_total,
    )
    if authorization is None:
        return "ERR research_write_not_authorized"
    if not _curator_researcher_owns_launch(
        conn,
        child_cid=_detect_self_cid() or "",
        pass_id=clean_id,
        batch_index=batch_index,
        batch_total=batch_total,
        research_name=authorization["research_name"],
    ):
        return "ERR research_write_not_authorized"
    target = CURATOR_REPORTS_DIR / authorization["research_name"]
    persisted = curator_research_payload(authorization, evidence)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        CURATOR_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        temporary.write_text(persisted, encoding="utf-8")
        temporary.replace(target)
        chmod_private_file(target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return f"ERR research_write_failed={exc}"
    digest = curator_report_sha256(persisted)
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                _ensure_session(conn),
                CURATOR_RESEARCH_PROVENANCE_KIND,
                str(target.resolve()),
                json.dumps(
                    {
                        "pass_id": clean_id,
                        "batch_index": batch_index,
                        "batch_total": batch_total,
                        "sha256": digest,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                int(time.time()),
            ),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("curator research provenance record failed", exc_info=True)
        return f"ERR research_provenance_failed={exc}"
    return f"ok path={target} chars={len(evidence)}"


@write_tool(idempotent=True)
def curator_report_write(
    pass_id: str = "",
    content: str = "",
    batch_index: int = 0,
    batch_total: int = 0,
    phase: str = "",
) -> str:
    """Atomically write one Curator report inside the configured report dir.

    This narrow tool is the cross-CLI alternative to direct filesystem Write:
    Codex workspace sandboxes cannot write ``~/.threadkeeper/curator`` when the
    project is elsewhere. ``pass_id`` is filename-safe and cannot select an
    arbitrary path. A bounded multi-child pass supplies ``batch_index`` and
    ``batch_total`` so children cannot overwrite each other's reports. Repeated
    calls replace the same pass/batch report so the Curator can persist its
    plan before mutation and then add actual validation/rollback results. Only
    a parent-authorized spawned Curator carrying the matching pass ID may write
    a report; the final content hash is recorded for the report applier.
    The verifier passes ``phase='verify'`` and writes its own ``VERIFY-*``
    report; only a child launched in the verify phase may do so.
    """
    supplied_id = pass_id.strip()
    if supplied_id and not _PASS_ID_RE.fullmatch(supplied_id):
        return "ERR invalid_pass_id"
    try:
        batch_index = int(batch_index)
        batch_total = int(batch_total)
    except (TypeError, ValueError):
        return "ERR invalid_batch"
    if bool(batch_index) != bool(batch_total):
        return "ERR invalid_batch"
    if batch_index and not (1 <= batch_index <= batch_total <= 9999):
        return "ERR invalid_batch"
    if len(content) > _MAX_REPORT_CHARS:
        return f"ERR report_too_large max_chars={_MAX_REPORT_CHARS}"
    if not content.strip():
        return "ERR empty_report"
    if WRITE_ORIGIN != "curator" or not SPAWNED_CHILD:
        return "ERR report_write_not_authorized"
    clean_id = current_pass_id()
    if not clean_id:
        return "ERR report_write_pass_mismatch"
    if supplied_id and supplied_id != clean_id:
        return "ERR report_write_pass_mismatch"
    child_phase = (
        VERIFY_PHASE
        if os.environ.get(PHASE_ENV, "").strip() == VERIFY_PHASE
        else EVALUATE_PHASE
    )
    requested_phase = phase.strip().lower()
    if requested_phase != (VERIFY_PHASE if child_phase == VERIFY_PHASE else ""):
        return "ERR report_write_phase_mismatch"
    conn = get_db()
    _ensure_session(conn)
    CURATOR_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_name = curator_report_name(
        clean_id, batch_index, batch_total, phase=child_phase,
    )
    if not _curator_writer_owns_launch(
        conn,
        child_cid=_detect_self_cid() or "",
        pass_id=clean_id,
        batch_index=batch_index,
        batch_total=batch_total,
        report_name=report_name,
    ):
        return "ERR report_write_not_authorized"
    target = CURATOR_REPORTS_DIR / report_name
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    persisted = content.rstrip() + "\n"
    try:
        temporary.write_text(persisted, encoding="utf-8")
        temporary.replace(target)
        chmod_private_file(target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return f"ERR report_write_failed={exc}"
    digest = curator_report_sha256(persisted)
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                _ensure_session(conn),
                CURATOR_REPORT_PROVENANCE_KIND,
                str(target.resolve()),
                f"pass_id={clean_id} sha256={digest}",
                int(time.time()),
            ),
        )
        _record_batch_report_provenance(
            conn,
            pass_id=clean_id,
            report_name=report_name,
            digest=digest,
            complete=CURATOR_REPORT_COMPLETE_MARKER in persisted,
            now=int(time.time()),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("curator report provenance record failed", exc_info=True)
        return f"ERR report_provenance_failed={exc}"
    return f"ok path={target} chars={len(content)}"


@write_tool(destructive=True, idempotent=True)
def curator_restore(
    pass_id: str = "",
    lesson_slug: str = "",
    skill_name: str = "",
) -> str:
    """Restore one lesson or skill from a curator pre-mutation snapshot.

    Pass exactly one of `lesson_slug` or `skill_name`. Restoring a lesson
    replaces the current same-slug section if present, otherwise re-adds it.
    Restoring a skill replaces the primary skill dir and mirrors it to the
    configured skill roots.
    """
    supplied_id = pass_id.strip()
    scoped_id = current_pass_id()
    if scoped_id and supplied_id and supplied_id != scoped_id:
        return "ERR restore_pass_mismatch"
    clean_id = scoped_id or supplied_id
    if not clean_id:
        return "ERR restore_pass_missing"
    conn = get_db()
    _ensure_session(conn)
    lesson_slug = lesson_slug.strip()
    skill_name = skill_name.strip()
    if bool(lesson_slug) == bool(skill_name):
        return "ERR pass exactly one of lesson_slug or skill_name"
    if refusal := _curator_restore_refusal(conn, lesson_slug, skill_name):
        return refusal
    if lesson_slug:
        return restore_lesson(clean_id, lesson_slug, conn)
    return restore_skill(clean_id, skill_name, conn)


def _curator_restore_refusal(conn, lesson_slug: str, skill_name: str) -> str:
    """A restore overwrites the live entry, so curator phases get the same
    limits as any other change to it."""
    if skill_name:
        from .skills import curator_skill_refusal

        return curator_skill_refusal(skill_name, "restore")
    from ..lessons import iter_lessons
    from .lessons import _curator_lesson_refusal

    current = next((it for it in iter_lessons() if it["slug"] == lesson_slug), None)
    if current is None:
        return verifier_scope_refusal(lesson_slug)
    return _curator_lesson_refusal(conn, current, "curator_restore")
