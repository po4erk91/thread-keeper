"""MCP tools for the evolve applier.

  evolve_apply(evolve_id)
    Implement a PROMOTED brief-format suggestion: spawn a child that edits
    brief.py, adds a golden render_brief test, runs the full suite, and opens
    a PR (never main). Manual trigger; always available.

  evolve_apply_curator_report(report_path="")
    Apply the latest complete Curator REPORT.md, or a specific report path,
    through the same single-flight evolve_applier role. This mutates memory
    stores directly through MCP tools; no code PR.

  evolve_mark_applied(evolve_id, pr_url)
    Called BY the applier child after `gh pr create` succeeds. Sets applied=1
    so the suggestion stops resurfacing. Requires a non-empty pr_url (the gate).

  evolve_mark_curator_report_applied(report_path, summary)
    Called BY the applier child after it processed a Curator report. Records an
    idempotency event so the report is not replayed.

  evolve_apply_status()
    Diagnostic: interval knob, promoted+unapplied queue, running applier, and
    the last few apply passes.
"""

from __future__ import annotations

import time

from .._mcp import mcp
from ..db import get_db
from ..identity import _ensure_session
from ..config import EVOLVE_APPLY_INTERVAL_S
from ..evolve_applier import (
    apply_curator_report,
    apply_evolve,
    mark_curator_report_applied,
    mark_applied,
    _pending_curator_reports,
    _promoted_unapplied,
    _running_applier_children,
    _last_apply_ts,
)


@mcp.tool()
def evolve_apply(evolve_id: int) -> str:
    """Implement a PROMOTED + not-yet-applied format-evolution suggestion.

    Spawns an `evolve_applier` child that: edits render_brief() in
    threadkeeper/brief.py to make the change; adds/extends a GOLDEN test
    asserting the new behavior appears AND the existing brief still renders;
    runs the FULL suite (`.venv/bin/python -m pytest -q`) until green; then
    opens a PULL REQUEST on a feature branch via `gh` — it NEVER pushes or
    commits to main (a human reviews + merges).

    applied=1 is set ONLY when the child reports a real PR back via
    evolve_mark_applied — opening the PR is the autonomy gate.

    Rejects ids that don't exist or aren't promoted+unapplied. Single-flight:
    refuses while another applier child is in flight. Returns a status line
    (spawned … / applier_running … / ERR …). Get ids from evolve_review()."""
    conn = get_db()
    _ensure_session(conn)
    return apply_evolve(int(evolve_id))


@mcp.tool()
def evolve_apply_curator_report(report_path: str = "") -> str:
    """Apply a Curator advisory report using the existing evolve_applier role.

    With no `report_path`, picks the latest complete
    `REPORT-*.md` in `THREADKEEPER_CURATOR_REPORTS_DIR` that has not already
    been marked applied. Single-flight: refuses while any evolve_applier child
    is in flight. The child may patch/delete memory through curated MCP tools,
    but does not edit code, use git, or open a PR."""
    conn = get_db()
    _ensure_session(conn)
    return apply_curator_report(report_path)


@mcp.tool()
def evolve_mark_applied(evolve_id: int, pr_url: str) -> str:
    """Mark a format-evolution suggestion as APPLIED — called by the
    evolve_applier child after it has opened the PR.

    Sets applied=1 (so the suggestion drops out of the brief / evolve_review)
    and records the PR url. `pr_url` is REQUIRED and must be non-empty: this is
    the PR gate — never mark a suggestion applied without a real pull request.
    A human still reviews + merges the PR."""
    if not (pr_url or "").strip():
        return ("ERR pr_url_required (PR gate: only mark applied once a real "
                "pull request exists)")
    conn = get_db()
    _ensure_session(conn)
    return mark_applied(conn, int(evolve_id), pr_url.strip())


@mcp.tool()
def evolve_mark_curator_report_applied(report_path: str, summary: str) -> str:
    """Mark a Curator report as processed by the evolve_applier child.

    The report must live under `THREADKEEPER_CURATOR_REPORTS_DIR`, match
    `REPORT-*.md`, and contain `CURATOR_PASS_COMPLETE`. This marker is the
    idempotency gate that prevents replaying the same advisory report."""
    if not (report_path or "").strip():
        return "ERR report_path_required"
    conn = get_db()
    _ensure_session(conn)
    return mark_curator_report_applied(conn, report_path.strip(), summary)


@mcp.tool()
def evolve_apply_status() -> str:
    """Show evolve-applier config + curator/evolve queues + running applier
    + the last 5 apply passes."""
    conn = get_db()
    _ensure_session(conn)
    reports = _pending_curator_reports(conn)
    pending = _promoted_unapplied(conn)
    running = _running_applier_children(conn)
    floor = _last_apply_ts(conn)
    now = int(time.time())
    age_s = (now - floor) if floor else None
    lines = [
        f"interval_s={EVOLVE_APPLY_INTERVAL_S:.0f} "
        f"curator_reports={len(reports)} "
        f"promoted_unapplied={len(pending)} "
        f"applier_running={len(running)}",
        f"cursor_ts={floor} (age={age_s}s)" if floor
        else "cursor_ts=0 (no prior pass)",
    ]
    if reports:
        lines.append("")
        lines.append("curator report pending:")
        for path in reports:
            lines.append(f"  {path}")
    if pending:
        lines.append("")
        lines.append("promoted+unapplied (oldest first):")
        for r in pending[:10]:
            snip = (r["suggestion"] or "")[:90].replace("\n", " ")
            lines.append(f"  #{r['id']}  {snip}")
    lines.append("")
    lines.append("recent apply events (newest first):")
    try:
        rows = conn.execute(
            "SELECT kind, created_at, summary FROM events "
            "WHERE kind IN ('evolve_apply_pass', 'curator_report_applied', "
            "'evolve_applied') "
            "ORDER BY created_at DESC, id DESC LIMIT 5"
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
            lines.append(f"  {age}s_ago  {r['kind']}: {snip}")
    return "\n".join(lines)
