"""Background daemon that watches ~/.claude/skills/*/SKILL.md for mtime
changes and updates skill_usage telemetry. Catches patches made via
external editors / direct Edit/Write tool calls that bypass skill_manage.

For a newly discovered skill, preserve the filesystem birth timestamp when
available; otherwise use its mtime, capped at the scan time. This keeps the
row's creation age distinct from its external-edit signal.

Tick interval is configurable; default 10s. Daemon thread, started lazily
on first _ensure_session() call so import-time side effects stay
minimal. Reads only — never writes to SKILL.md.
"""

from __future__ import annotations
import logging
import os
import threading
import time
from typing import Optional

from .config import BACKGROUND_DAEMONS_ALLOWED, CLAUDE_SKILLS_DIR
from .db import get_db
from .helpers import daemon_sleep

logger = logging.getLogger(__name__)

_started = False
_tick_interval_s = float(os.environ.get(
    'THREADKEEPER_SKILL_WATCH_INTERVAL_S', '10'))


def _created_at(stat_result: os.stat_result, now: int) -> int:
    """Return the best available creation timestamp for a discovered skill."""
    birthtime = getattr(stat_result, "st_birthtime", None)
    if birthtime is not None and birthtime > 0:
        return int(birthtime)
    return min(int(stat_result.st_mtime), now)


def _scan_once(conn) -> int:
    """Scan ~/.claude/skills/*/SKILL.md mtimes. For each file whose mtime
    is newer than skill_usage.last_patched_at (or row missing), bump
    last_patched_at + patch_count. Returns number of rows updated.
    """
    if not CLAUDE_SKILLS_DIR.exists():
        return 0
    updates = 0
    for skill_dir in CLAUDE_SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        if skill_dir.name.startswith('.'):  # skip .archive
            continue
        md = skill_dir / 'SKILL.md'
        if not md.exists():
            continue
        try:
            stat_result = md.stat()
        except OSError:
            continue
        mtime = int(stat_result.st_mtime)
        created_at = _created_at(stat_result, int(time.time()))
        name = skill_dir.name
        # Ensure row exists; insert with foreground origin if not present
        # (this is a user-edited skill, not agent-created).
        conn.execute(
            "INSERT INTO skill_usage (name, created_at, created_by_origin) "
            "VALUES (?, ?, 'foreground') ON CONFLICT(name) DO NOTHING",
            (name, created_at),
        )
        row = conn.execute(
            "SELECT last_patched_at FROM skill_usage WHERE name=?",
            (name,),
        ).fetchone()
        prev = row['last_patched_at'] if row and row['last_patched_at'] else 0
        if mtime > prev:
            conn.execute(
                "UPDATE skill_usage SET last_patched_at=?, "
                "patch_count=patch_count+1 WHERE name=?",
                (mtime, name),
            )
            updates += 1
    if updates:
        conn.commit()
    return updates


def _watch_loop() -> None:
    while True:
        try:
            conn = get_db()
            try:
                _scan_once(conn)
            finally:
                conn.close()
        except Exception:
            logger.debug("skill_watcher tick failed", exc_info=True)
        daemon_sleep(_tick_interval_s)


def start_skill_watcher() -> None:
    """Start the daemon if not already running. Safe to call multiple times."""
    global _started
    if _started:
        return
    if _tick_interval_s <= 0:
        return
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(
        target=_watch_loop, name='skill_watcher', daemon=True,
    )
    t.start()
    _started = True
