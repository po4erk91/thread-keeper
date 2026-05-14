"""Session lifecycle close-out: mark the active session ended and optionally
record a terse summary note for future briefs."""

import sqlite3
import time

from .._mcp import mcp
from ..db import get_db
from ..helpers import fmt_age
from ..embeddings import _embed
from .. import identity


@mcp.tool()
def session_end(summary: str = "") -> str:
    """Mark current session ended with optional terse summary."""
    if identity._session_id is None:
        return "no_active_session"
    conn = get_db()
    now = int(time.time())
    sid = identity._session_id
    started = identity._session_start or now
    conn.execute("UPDATE sessions SET ended_at=? WHERE id=?", (now, sid))
    if summary:
        emb = _embed(summary)
        conn.execute(
            "INSERT INTO notes (thread_id, content, kind, created_at, session_id, embedding) "
            "VALUES (NULL,?,?,?,?,?)",
            (summary, "session_summary", now, sid, emb),
        )
    conn.commit()
    identity._session_id = None
    identity._session_start = None
    return f"closed sess={sid} dur={fmt_age(now - started)}"
