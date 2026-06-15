"""MCP tools for hot-config reload (issue #2).

  config_reload(force=True)
    Re-read ~/.claude/settings.json NOW and republish changed knobs across
    the live process. `force=True` (default) bypasses the mtime/interval
    gates so a manual call always re-reads.

  config_watch_status()
    Diagnostic snapshot: watched file, interval, cursor, applied keys, and
    the last few reload passes.
"""

from __future__ import annotations

import time

from .._mcp import mcp
from ..db import get_db
from ..identity import _ensure_session
from .. import config
from .. import config_watcher


@mcp.tool()
def config_reload(force: bool = True) -> str:
    """Re-read the watched settings file and hot-apply changed env knobs.

    Mirrors the threadkeeper-relevant `env` keys from
    `~/.claude/settings.json` into the live process (no Claude Code restart),
    then republishes the changed constants to every daemon and tool. Newly
    enabled daemons are started; changed intervals take effect on the next
    daemon tick. Returns the pass outcome (e.g. `reloaded changed=2 ...`).
    """
    conn = get_db()
    _ensure_session(conn)
    return config_watcher.run_config_watch_pass(force=force)


@mcp.tool()
def config_watch_status() -> str:
    """Show hot-config-reload state + the last 5 reload passes."""
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    path = config_watcher._settings_path()
    last_mtime = config_watcher._last_mtime
    applied = sorted(config_watcher._applied_keys)
    lines = [
        f"interval_s={config.CONFIG_WATCH_INTERVAL_S:.0f} "
        f"file={path} exists={path.exists()}",
        f"cursor_mtime={last_mtime if last_mtime is not None else '(none)'} "
        f"applied_keys={len(applied)}",
    ]
    if applied:
        lines.append("  " + ", ".join(applied))
    lines.append("")
    lines.append("recent passes (newest first):")
    try:
        rows = conn.execute(
            "SELECT created_at, summary FROM events "
            "WHERE kind='config_watch_pass' "
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
