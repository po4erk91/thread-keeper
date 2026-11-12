"""Helpful and harmful counters per skill.

A skill use is *corrected* (harmful) when one of the next user turns in the
same session corrects the result; otherwise it counts as *helped*. Explicit
``skill_record(outcome='wrong')`` reports also count as harmful. Retirement
and replacement decisions read :func:`contribution`.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import time

from .i18n import CORRECTION_RE

CORRECTION_WINDOW_TURNS = 3
OUTCOME_SETTLE_S = 3600
CLASSIFY_BATCH = 500


@dataclass(frozen=True)
class Contribution:
    helpful: int
    harmful: int

    @property
    def score(self) -> int:
        return self.helpful - self.harmful


def _next_user_turns(conn: sqlite3.Connection, session_id: str, after: int) -> list[str]:
    rows = conn.execute(
        "SELECT content FROM dialog_messages WHERE session_id=? AND role='user' "
        "AND created_at>? ORDER BY created_at LIMIT ?",
        (session_id, after, CORRECTION_WINDOW_TURNS),
    ).fetchall()
    return [str(row["content"] or "") for row in rows]


def outcome_after_use(conn: sqlite3.Connection, session_id: str, used_at: int) -> str:
    """'corrected' when a following user turn corrects the result, else 'helped'."""
    turns = _next_user_turns(conn, session_id, used_at)
    return "corrected" if any(CORRECTION_RE.search(turn) for turn in turns) else "helped"


def classify_pending_outcomes(conn: sqlite3.Connection, *, now: int | None = None,
                              settle_s: int = OUTCOME_SETTLE_S,
                              limit: int = CLASSIFY_BATCH) -> int:
    """Attach an outcome to settled foreground uses. Returns rows classified.

    A use is settled after ``settle_s`` so the session's next turns exist.
    """
    cutoff = int(now if now is not None else time.time()) - settle_s
    try:
        rows = conn.execute(
            "SELECT id, session_id, created_at FROM skill_use_events "
            "WHERE outcome IS NULL AND foreground=1 AND session_id!='' "
            "AND created_at<=? ORDER BY created_at LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    stamp = int(time.time())
    for row in rows:
        outcome = outcome_after_use(conn, row["session_id"], int(row["created_at"]))
        conn.execute(
            "UPDATE skill_use_events SET outcome=?, outcome_at=? WHERE id=?",
            (outcome, stamp, row["id"]),
        )
    if rows:
        conn.commit()
    return len(rows)


def contribution(conn: sqlite3.Connection, name: str) -> Contribution:
    """Clean foreground uses vs corrected uses plus explicit 'wrong' reports.

    Uses recorded before the ledger existed have no outcome; they count as
    helpful unless reported wrong, so an old, heavily used skill is not
    retired for a handful of recent corrections.
    """
    usage = conn.execute(
        "SELECT COALESCE(foreground_use_count, 0) AS uses, COALESCE(wrong_count, 0) AS wrong "
        "FROM skill_usage WHERE name=?",
        (name,),
    ).fetchone()
    try:
        corrected_row = conn.execute(
            "SELECT COUNT(*) FROM skill_use_events WHERE skill_name=? AND outcome='corrected'",
            (name,),
        ).fetchone()
    except sqlite3.OperationalError:
        corrected_row = (0,)
    corrected = int(corrected_row[0] if corrected_row else 0)
    uses = int(usage["uses"]) if usage else 0
    wrong = int(usage["wrong"]) if usage else 0
    harmful = corrected + wrong
    return Contribution(helpful=max(0, uses - harmful), harmful=harmful)


def is_harmful(item: Contribution, min_harmful: int) -> bool:
    """Used, but followed by more corrections than clean uses."""
    return item.harmful >= min_harmful and item.harmful > item.helpful
