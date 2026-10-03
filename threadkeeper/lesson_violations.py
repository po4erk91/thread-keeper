"""Repeated-violation tracking for class-level lessons (#228).

A lesson is passive memory: an agent has to read it and choose to follow it.
When the same rule keeps being broken even though a lesson already covers it,
more memory is not the fix — the rule belongs in an active guard such as a
PreToolUse hook. The learning loops record each observed repeat with
`lesson_violation`; once a lesson collects LESSON_VIOLATION_THRESHOLD distinct
violations inside the window it is `memory-insufficient`, and the dashboard
and Curator recommend escalating it to enforcement.

Violations are node-local `events` rows. One conversation counts at most once
per lesson per day, so a single long session cannot inflate a count.
"""
from __future__ import annotations

import sqlite3
import time

from .config import LESSON_VIOLATION_THRESHOLD, LESSON_VIOLATION_WINDOW_DAYS

LESSON_VIOLATION_KIND = "lesson_violation"


def _window_start(now: int) -> int:
    return now - max(1, int(LESSON_VIOLATION_WINDOW_DAYS)) * 86400


def violation_count(conn: sqlite3.Connection, slug: str, now: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind=? AND target=? AND created_at>=?",
        (LESSON_VIOLATION_KIND, slug, _window_start(now)),
    ).fetchone()
    return int(row[0] or 0)


def record_violation(
    conn: sqlite3.Connection,
    slug: str,
    evidence: str,
    *,
    session_id: str,
    origin: str,
    now: int | None = None,
) -> tuple[int, bool]:
    """Record one observed repeat. Returns (count_in_window, recorded)."""
    now = int(time.time()) if now is None else int(now)
    day = now // 86400
    duplicate = conn.execute(
        "SELECT 1 FROM events WHERE kind=? AND target=? AND session_id=? "
        "AND created_at/86400=?",
        (LESSON_VIOLATION_KIND, slug, session_id, day),
    ).fetchone()
    if duplicate is None:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                session_id, LESSON_VIOLATION_KIND, slug,
                f"origin={origin} evidence={' '.join(evidence.split())[:280]}",
                now,
            ),
        )
        conn.commit()
    return violation_count(conn, slug, now), duplicate is None


def memory_insufficient(
    conn: sqlite3.Connection, now: int | None = None,
) -> dict[str, int]:
    """Lessons at or above the violation threshold -> violation count."""
    now = int(time.time()) if now is None else int(now)
    try:
        rows = conn.execute(
            "SELECT target, COUNT(*) AS n FROM events WHERE kind=? "
            "AND created_at>=? GROUP BY target HAVING n>=?",
            (
                LESSON_VIOLATION_KIND, _window_start(now),
                max(1, int(LESSON_VIOLATION_THRESHOLD)),
            ),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["target"]: int(row["n"]) for row in rows}


def violation_counts(
    conn: sqlite3.Connection, now: int | None = None,
) -> dict[str, int]:
    """Every lesson with at least one violation in the window -> count."""
    now = int(time.time()) if now is None else int(now)
    try:
        rows = conn.execute(
            "SELECT target, COUNT(*) AS n FROM events WHERE kind=? "
            "AND created_at>=? GROUP BY target",
            (LESSON_VIOLATION_KIND, _window_start(now)),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["target"]: int(row["n"]) for row in rows}
