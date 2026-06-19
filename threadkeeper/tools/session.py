"""Session lifecycle close-out: mark the active session ended and optionally
record a terse summary note for future briefs."""

import sqlite3
import time

from .._mcp import read_tool, write_tool
from ..db import get_db
from ..helpers import fmt_age
from ..embeddings import _embed, embed_tag
from .. import identity


@write_tool(idempotent=True)
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
            "INSERT INTO notes (thread_id, content, kind, created_at, session_id, "
            "embedding, embed_backend) VALUES (NULL,?,?,?,?,?,?)",
            (summary, "session_summary", now, sid, emb, embed_tag(emb)),
        )
    conn.commit()
    identity._session_id = None
    identity._session_start = None
    return f"closed sess={sid} dur={fmt_age(now - started)}"
