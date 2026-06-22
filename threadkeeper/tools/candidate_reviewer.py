"""MCP tools for the candidate-reviewer daemon.

  candidate_review_run(force=False, dry_run=False)
    Fire one review pass NOW. dry_run shows inventory without spawn.

  candidate_review_status()
    Diagnostic: env config, pending queue size, last 5 passes.
"""

from __future__ import annotations

import time

from .._mcp import read_tool, write_tool
from ..db import get_db
from ..identity import _ensure_session
from ..candidate_reviewer import (
    CANDIDATE_REVIEW_PROMPT,
    _collect_pending,
    _last_review_ts,
    run_review_pass,
)
from ..config import (
    CANDIDATE_REVIEW_INTERVAL_S,
    CANDIDATE_REVIEW_MIN,
)


@write_tool()
def candidate_review_run(force: bool = False, dry_run: bool = False) -> str:
    """Fire one candidate-review pass.

    `force=True` runs even when CANDIDATE_REVIEW_INTERVAL_S=0
    (daemon disabled).

    `dry_run=True` short-circuits before the spawn — returns the
    inventory and pending count. No spawn, no cursor advance.
    """
    conn = get_db()
    _ensure_session(conn)
    if dry_run:
        inventory, n = _collect_pending(conn)
        head = inventory[:2000]
        suffix = "…(truncated for display)" if len(inventory) > 2000 else ""
        below = n < CANDIDATE_REVIEW_MIN
        return (
            f"dry_run: pending={n} min={CANDIDATE_REVIEW_MIN} "
            f"would_spawn={'no (below_threshold)' if below else 'yes'}\n\n"
            f"--- prompt preview ---\n"
            f"{CANDIDATE_REVIEW_PROMPT[:400]}…\n\n"
            f"--- inventory head ---\n{head}{suffix}"
        )
    return run_review_pass(force=force)


@read_tool()
def candidate_review_status() -> str:
    """Show candidate-reviewer configuration + last 5 passes +
    current pending queue size."""
    conn = get_db()
    _ensure_session(conn)
    floor = _last_review_ts(conn)
    now = int(time.time())
    age_s = (now - floor) if floor else None
    try:
        n_pending = conn.execute(
            "SELECT COUNT(*) FROM extract_candidates WHERE status='pending'"
        ).fetchone()[0]
    except Exception:
        n_pending = "?"
    lines = [
        f"interval_s={CANDIDATE_REVIEW_INTERVAL_S:.0f} "
        f"min={CANDIDATE_REVIEW_MIN} pending_now={n_pending}",
        f"cursor_ts={floor} (age={age_s}s)" if floor
        else "cursor_ts=0 (no prior pass)",
        "",
        "recent passes (newest first):",
    ]
    try:
        rows = conn.execute(
            "SELECT created_at, summary FROM events "
            "WHERE kind='candidate_review_pass' "
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
    return "\n".join(lines)
