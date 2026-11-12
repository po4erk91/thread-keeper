"""Find other sessions where a reviewed topic came up.

A closed thread is usually one episode. A new skill needs the same task in
at least two sessions or days, so the thread reviewer gets a short list of
other real sessions whose user turns match the thread by meaning. It passes
those ids as ``evidence_sessions`` when it creates a skill.
"""

from __future__ import annotations

import sqlite3
import time

MIN_SCORE = 0.55
MAX_SESSIONS = 6


def related_sessions(conn: sqlite3.Connection, query: str, *,
                     min_score: float = MIN_SCORE,
                     limit: int = MAX_SESSIONS) -> list[tuple[str, str, float]]:
    """(session id, day, best score) of real sessions matching ``query``."""
    from .embeddings import _dialog_cosine_search
    from .harvest import is_harvest_excluded_session

    try:
        if not query.strip() or not conn.execute(
            "SELECT 1 FROM dialog_messages WHERE role='user' LIMIT 1"
        ).fetchone():
            return []
        hits = _dialog_cosine_search(conn, query, 40, role="user")
    except (sqlite3.OperationalError, ValueError):
        return []
    best: dict[str, tuple[str, float]] = {}
    for hit in hits:
        session = str(hit.get("session_id") or "")
        score = float(hit.get("score") or 0.0)
        if not session or score < min_score or session in best:
            continue
        if is_harvest_excluded_session(conn, session):
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(int(hit.get("created_at") or 0)))
        best[session] = (day, score)
    ranked = sorted(best.items(), key=lambda item: -item[1][1])[:limit]
    return [(session, day, score) for session, (day, score) in ranked]


def related_sessions_block(conn: sqlite3.Connection, query: str) -> str:
    """Prompt block listing related sessions, or a single-episode note."""
    sessions = related_sessions(conn, query)
    if len(sessions) < 2:
        return (
            "RELATED SESSIONS: fewer than two sessions match this topic, so it "
            "is a single episode — write a compact lesson, not a new skill.\n\n"
        )
    lines = ["RELATED SESSIONS (pass two or more as evidence_sessions when creating):"]
    lines += [f"  - session={session} day={day} score={score:.2f}" for session, day, score in sessions]
    return "\n".join(lines) + "\n\n"
