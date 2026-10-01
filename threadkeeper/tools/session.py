"""Session lifecycle close-out: mark the active session ended and optionally
record a terse summary note for future briefs."""

import sqlite3
import time

from .._mcp import write_tool
from ..db import run_write
from ..helpers import fmt_age
from ..embeddings import _embed, _vec_upsert_note, embed_tag
from ..memory_compat import memory_provenance
from .. import identity
from ..config import WRITE_ORIGIN
from ..authority import authority_for_origin, record_origin_root


@write_tool(idempotent=True)
def session_end(summary: str = "") -> str:
    """Mark current session ended with optional terse summary."""
    if identity._session_id is None:
        return "no_active_session"
    if summary and authority_for_origin(WRITE_ORIGIN) is None:
        return "ERR authority_unknown_origin"
    now = int(time.time())
    sid = identity._session_id
    started = identity._session_start or now
    emb = _embed(summary) if summary else None

    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE sessions SET ended_at=? WHERE id=?", (now, sid))
        if summary:
            cur = conn.execute(
                "INSERT INTO notes (thread_id, content, kind, created_at, "
                "session_id, embedding, embed_backend) "
                "VALUES (NULL,?,?,?,?,?,?)",
                (summary, "session_summary", now, sid, emb, embed_tag(emb)),
            )
            if not record_origin_root(
                conn, "note", str(cur.lastrowid), write_origin=WRITE_ORIGIN,
                principal=sid, channel="mcp:session-summary",
            ):
                raise ValueError("authority_stamp_failed")
            _vec_upsert_note(conn, cur.lastrowid, emb)
            memory_provenance(
                conn, "note", cur.lastrowid, source_event_kind="session_end",
                source_event_id=sid,
            )

    run_write("session-end", _write)
    identity._session_id = None
    identity._session_start = None
    return f"closed sess={sid} dur={fmt_age(now - started)}"
