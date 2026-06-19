"""Manual run/status MCP tools for the dialectic auto-feed daemons.

  dialectic_mine_run(force=True)        — capture user replies now
  dialectic_validate_run(force, dry_run)— interpret the buffer now
  dialectic_mine_status()               — miner config + buffer + last passes
  dialectic_validate_status()           — validator config + pending + passes
"""
from __future__ import annotations

import time

from .._mcp import mcp
from ..db import get_db
from ..identity import _ensure_session
from ..dialectic_miner import run_mine_pass, _last_mine_rowid
from ..dialectic_validator import (
    run_validate_pass, _collect_pending, _last_validate_ts,
)
from ..config import (
    DIALECTIC_MINE_INTERVAL_S,
    DIALECTIC_VALIDATE_BATCH_SIZE,
    DIALECTIC_VALIDATE_INTERVAL_S,
    DIALECTIC_VALIDATE_MIN,
)


@mcp.tool()
def dialectic_mine_run(force: bool = True) -> str:
    """Fire one mechanical capture pass now (force=True runs even when the
    miner daemon interval is 0)."""
    conn = get_db()
    _ensure_session(conn)
    return run_mine_pass(force=force)


@mcp.tool()
def dialectic_validate_run(force: bool = True, dry_run: bool = False) -> str:
    """Fire one validator pass. dry_run shows pending count + would_spawn
    without spawning or advancing the cursor."""
    conn = get_db()
    _ensure_session(conn)
    if dry_run:
        _, batch_n, total_n, _ = _collect_pending(conn)
        below = total_n < DIALECTIC_VALIDATE_MIN
        return (
            f"dry_run: pending={total_n} batch={batch_n} "
            f"batch_size={DIALECTIC_VALIDATE_BATCH_SIZE} "
            f"min={DIALECTIC_VALIDATE_MIN} "
            f"would_spawn={'no (below_threshold)' if below else 'yes'}"
        )
    return run_validate_pass(force=force)


@mcp.tool()
def dialectic_mine_status() -> str:
    """Miner config + buffer sizes + last 5 capture passes."""
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM dialectic_observations WHERE status='pending'"
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM dialectic_observations"
        ).fetchone()[0]
    except Exception:
        pending = total = "?"
    floor = _last_mine_rowid(conn)
    lines = [
        f"interval_s={DIALECTIC_MINE_INTERVAL_S:.0f} buffer_pending={pending} "
        f"buffer_total={total}",
        f"cursor_rowid={floor}" if floor else "cursor_rowid=0 (no prior pass)",
        "",
        "recent passes (newest first):",
    ]
    rows = conn.execute(
        "SELECT created_at, summary FROM events WHERE kind='dialectic_mine_pass' "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()
    lines += [f"  {now - int(r['created_at'])}s_ago  {(r['summary'] or '')[:120]}"
              for r in rows] or ["  (none)"]
    return "\n".join(lines)


@mcp.tool()
def dialectic_validate_status() -> str:
    """Validator config + pending observation count + last 5 passes."""
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM dialectic_observations "
            "WHERE status='pending' AND claimed_at IS NULL"
        ).fetchone()[0]
        claimed = conn.execute(
            "SELECT COUNT(*) FROM dialectic_observations "
            "WHERE status='pending' AND claimed_at IS NOT NULL"
        ).fetchone()[0]
    except Exception:
        pending = claimed = "?"
    floor = _last_validate_ts(conn)
    lines = [
        f"interval_s={DIALECTIC_VALIDATE_INTERVAL_S:.0f} "
        f"min={DIALECTIC_VALIDATE_MIN} "
        f"batch_size={DIALECTIC_VALIDATE_BATCH_SIZE} "
        f"pending_now={pending} claimed_now={claimed}",
        f"cursor_ts={floor}" if floor else "cursor_ts=0 (no prior pass)",
        "",
        "recent passes (newest first):",
    ]
    rows = conn.execute(
        "SELECT created_at, summary FROM events "
        "WHERE kind='dialectic_validate_pass' ORDER BY id DESC LIMIT 5"
    ).fetchall()
    lines += [f"  {now - int(r['created_at'])}s_ago  {(r['summary'] or '')[:120]}"
              for r in rows] or ["  (none)"]
    return "\n".join(lines)
