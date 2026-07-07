"""MCP tools for hot-config reload (issue #2 + #133).

  config_reload(force=True)
    Re-read the watched config files NOW and republish changed knobs across
    the live process. `force=True` (default) bypasses the mtime/interval
    gates so a manual call always re-reads.

  config_watch_status()
    Diagnostic snapshot: watched files (universal env-file + host CLI file),
    interval, cursors, applied/shadowed keys, and the last few reload passes.
"""

from __future__ import annotations

import time

from .._mcp import read_tool, write_tool
from ..db import get_db
from ..identity import _ensure_session
from .. import config
from .. import config_watcher


@write_tool(idempotent=True)
def config_reload(force: bool = True) -> str:
    """Re-read the watched config files and hot-apply changed env knobs.

    Re-reads the universal env-file (`~/.threadkeeper/.env`, every host) and
    the host CLI's own env-block file (Claude Code → `~/.claude/settings.json`)
    into the live process — no CLI restart — then republishes the changed
    constants to every daemon and tool. Newly enabled daemons are started;
    changed intervals take effect on the next daemon tick. Returns the pass
    outcome (e.g. `env=reloaded changed=2 ... cli=unchanged`).
    """
    conn = get_db()
    _ensure_session(conn)
    return config_watcher.run_config_watch_pass(force=force)


@read_tool()
def config_watch_status() -> str:
    """Show hot-config-reload state + the last 5 reload passes.

    In hybrid mode reports both watched files: the universal env-file
    (`~/.threadkeeper/.env`, all hosts) and the host CLI's env-block file
    (resolved via identity). `THREADKEEPER_CONFIG_WATCH_PATH` pins a single
    file (legacy single-file mode).
    """
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    cw = config_watcher

    def _cur(m: object) -> str:
        return f"{m:.3f}" if isinstance(m, (int, float)) else "(none)"

    single = bool(config.CONFIG_WATCH_PATH)
    lines = [
        f"interval_s={config.CONFIG_WATCH_INTERVAL_S:.0f} "
        f"mode={'single-file' if single else 'hybrid'}"
    ]

    # Universal env-file target (skipped in single-file/escape-hatch mode).
    if not single:
        env = cw._env_file_path()
        lines.append(
            f"env_file={env} exists={env.exists()} "
            f"cursor_mtime={_cur(cw._env_last_mtime)}"
        )

    # Host CLI env-block file target.
    cli = cw._cli_settings_path()
    if cli is not None:
        applied = sorted(cw._applied_keys)
        shadowed = sorted(cw._shadowed_keys)
        lines.append(
            f"cli_file={cli} exists={cli.exists()} "
            f"cursor_mtime={_cur(cw._last_mtime)} "
            f"applied_keys={len(applied)} shadowed_keys={len(shadowed)}"
        )
        if applied:
            lines.append("  applied: " + ", ".join(applied))
        if shadowed:
            lines.append("  shadowed (higher-scope pins): " + ", ".join(shadowed))
    else:
        host = cw.identity.active_cli() or "(unknown)"
        lines.append(f"cli_file=(none — host {host!r} hot-reloads via env_file)")

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
