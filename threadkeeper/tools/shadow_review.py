"""MCP tools for the shadow-review machinery.

  shadow_review_run(force=False, dry_run=False)
    Trigger one shadow pass NOW. `force=True` overrides the
    SHADOW_REVIEW_INTERVAL_S=0 disable. `dry_run=True` returns the prompt
    that WOULD be spawned (no actual spawn) — useful for inspecting
    candidate windows or building tests.

  shadow_review_status()
    Diagnostic snapshot: env config, cursor position, last 5 passes.
"""

from __future__ import annotations

import time
from typing import Optional

from .._mcp import mcp
from ..db import get_db
from ..identity import _ensure_session
from ..shadow_review import (
    SHADOW_REVIEW_PROMPT,
    _collect_window,
    _last_shadow_ts,
    _record_shadow_pass,
    run_shadow_pass,
)
from ..config import (
    SHADOW_REVIEW_INTERVAL_S,
    SHADOW_REVIEW_MIN_CHARS,
    SHADOW_REVIEW_WINDOW_S,
)


@mcp.tool()
def shadow_review_run(force: bool = False, dry_run: bool = False) -> str:
    """Fire one shadow-review pass.

    `force=True` runs even when the daemon is disabled (interval=0). Used
    by tests and one-shot triage.

    `dry_run=True` short-circuits before the spawn — returns the dialog
    dump that WOULD be evaluated, plus n_chars and high-water cursor. No
    spawn. No cursor advance. Use this to inspect candidate windows
    before paying for an evaluator child.
    """
    conn = get_db()
    _ensure_session(conn)
    if dry_run:
        floor = _last_shadow_ts(conn)
        dump, high_water, n_chars = _collect_window(
            conn, floor, SHADOW_REVIEW_WINDOW_S,
        )
        if n_chars == 0:
            return "dry_run: no_window (nothing new since last cursor)"
        head = dump[:2000]
        suffix = "…(truncated for display)" if len(dump) > 2000 else ""
        return (
            f"dry_run: n_chars={n_chars} high_water_ts={high_water} "
            f"min_chars={SHADOW_REVIEW_MIN_CHARS} "
            f"would_spawn={'yes' if n_chars >= SHADOW_REVIEW_MIN_CHARS else 'no'}\n\n"
            f"--- prompt preview ---\n"
            f"{SHADOW_REVIEW_PROMPT[:400]}…\n\n"
            f"--- dialog window head ---\n{head}{suffix}"
        )
    return run_shadow_pass(force=force)


@mcp.tool()
def shadow_review_status() -> str:
    """Show shadow-review configuration + last 5 passes.

    Snapshot for sanity-checking that the daemon is alive and
    advancing its cursor. Counts how many spawned passes vs how many
    were skipped for being too short or empty."""
    conn = get_db()
    _ensure_session(conn)
    floor = _last_shadow_ts(conn)
    now = int(time.time())
    age_s = (now - floor) if floor else None
    lines = [
        f"interval_s={SHADOW_REVIEW_INTERVAL_S:.0f} "
        f"window_s={SHADOW_REVIEW_WINDOW_S} "
        f"min_chars={SHADOW_REVIEW_MIN_CHARS}",
        f"cursor_ts={floor} (age={age_s}s)" if floor
        else "cursor_ts=0 (no prior pass)",
        "",
        "recent passes (newest first):",
    ]
    try:
        rows = conn.execute(
            "SELECT created_at, summary FROM events "
            "WHERE kind='shadow_review_pass' "
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
