"""Stylistic running rules and verbatim user quotes."""

import sqlite3
import time
from .._mcp import write_tool
from ..db import run_write
from .. import identity
from ..identity import _emit
from ..project_scope import resolve_write_scope


@write_tool()
def verbatim_user(content: str, thread_id: str = "") -> str:
    """Capture a user quote worth surfacing in future briefs. Use when the user's
    exact phrasing matters (sharp reframes, decisions, pushback)."""
    identity.ensure_session_started()
    now = int(time.time())
    tid = thread_id.strip() or None

    def _write(conn: sqlite3.Connection) -> str:
        thread = None
        if tid:
            thread = conn.execute(
                "SELECT project_id, memory_scope FROM threads WHERE id=?", (tid,)
            ).fetchone()
            if thread is None:
                return f"ERR thread_not_found={tid}"
        scope, project_id = (
            (thread["memory_scope"], thread["project_id"])
            if thread else resolve_write_scope()
        )
        conn.execute(
            "INSERT INTO verbatim (speaker, content, thread_id, created_at, "
            "session_id, project_id, memory_scope) VALUES (?,?,?,?,?,?,?)",
            ("user", content, tid, now, identity._session_id, project_id, scope),
        )
        _emit(conn, "verbatim_user", target=tid, summary=content)
        return "ok"

    return run_write("verbatim-user", _write)


@write_tool(idempotent=True)
def style_set(key: str, value: str) -> str:
    """Set a stylistic running rule. Examples:
       lang=ru | prose=lean | allow=half-baked,weird | deny=sycophancy,headers"""
    identity.ensure_session_started()
    now = int(time.time())

    def _write(conn: sqlite3.Connection) -> str:
        conn.execute(
            "INSERT INTO style (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, now),
        )
        _emit(conn, "style_set", target=key, summary=f"{key}={value}")
        return "ok"

    return run_write("style-set", _write)
