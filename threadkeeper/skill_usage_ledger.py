"""Per-use skill ledger shared by every CLI adapter.

``skill_usage`` keeps cumulative counters. This ledger keeps one row per
observed use so that:

* re-reading a transcript never double-counts (UNIQUE key);
* uses from CLIs without a Skill tool (Codex, Gemini/Antigravity, Copilot)
  reach the same counters as Claude Code;
* later passes can attach an outcome (helped / corrected) to each use.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .skill_signals import SkillSignal, known_only, without_bulk_reads


def insert_use_event(
    conn: sqlite3.Connection, name: str, source: str, session_id: str,
    signal: str, created_at: int, foreground: bool,
) -> bool:
    """Insert one ledger row; True when it is new."""
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO skill_use_events "
            "(skill_name, source, session_id, signal, created_at, foreground) "
            "VALUES (?,?,?,?,?,?)",
            (name, source, session_id or "", signal, int(created_at), int(foreground)),
        )
    except sqlite3.OperationalError:
        return False
    return cur.rowcount > 0


def _bump_counters(conn: sqlite3.Connection, name: str, created_at: int, foreground: bool) -> None:
    conn.execute(
        "INSERT INTO skill_usage (name, created_at, created_by_origin) "
        "VALUES (?, ?, 'foreground') ON CONFLICT(name) DO NOTHING",
        (name, created_at),
    )
    conn.execute(
        "UPDATE skill_usage SET use_count=use_count+1, "
        "foreground_use_count=foreground_use_count+?, "
        "last_used_at=MAX(COALESCE(last_used_at, 0), ?), "
        "state=CASE WHEN state='stale' THEN 'active' ELSE state END "
        "WHERE name=? AND classification='production'",
        (int(foreground), created_at, name),
    )


def _recompute_tier(conn: sqlite3.Connection, name: str, created_at: int) -> None:
    try:
        from .tools.skills import _recompute_skill_tier
        _recompute_skill_tier(conn, name, created_at)
    except (ImportError, sqlite3.OperationalError):
        pass


def is_known_skill(conn: sqlite3.Connection, name: str, roots: Iterable[Path]) -> bool:
    """A name counts only when it is a tracked or installed skill."""
    try:
        if conn.execute("SELECT 1 FROM skill_usage WHERE name=?", (name,)).fetchone():
            return True
    except sqlite3.OperationalError:
        return False
    return any((root / name / "SKILL.md").is_file() for root in roots)


def record_signals(
    conn: sqlite3.Connection, source: str, signals: Iterable[SkillSignal],
    *, roots: Iterable[Path], is_excluded_session,
) -> int:
    """Count new, non-bulk, known-skill signals from real sessions.

    Sessions spawned by thread-keeper (reviewers, curators, auditors) are
    skipped entirely: the system reading its own skills is not a use.
    Returns the number of new uses counted.
    """
    root_list = list(roots)
    usable = known_only(
        without_bulk_reads(signals),
        lambda name: is_known_skill(conn, name, root_list),
    )
    excluded: dict[str, bool] = {}
    counted = 0
    for item in usable:
        if item.session_id not in excluded:
            excluded[item.session_id] = bool(is_excluded_session(conn, item.session_id))
        if excluded[item.session_id]:
            continue
        if not insert_use_event(
            conn, item.name, source, item.session_id, item.signal,
            item.created_at, foreground=True,
        ):
            continue
        _bump_counters(conn, item.name, item.created_at, foreground=True)
        _recompute_tier(conn, item.name, item.created_at)
        counted += 1
    return counted
