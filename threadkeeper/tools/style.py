"""Stylistic running rules and verbatim user quotes."""

import sqlite3
import time
from .._mcp import write_tool
from ..db import run_write
from .. import identity
from ..identity import _emit


@write_tool()
def verbatim_user(content: str, thread_id: str = "") -> str:
    """Capture a user quote worth surfacing in future briefs. Use when the user's
    exact phrasing matters (sharp reframes, decisions, pushback)."""
    identity.ensure_session_started()
    now = int(time.time())
    tid = thread_id.strip() or None

    def _write(conn: sqlite3.Connection) -> str:
        conn.execute(
            "INSERT INTO verbatim (speaker, content, thread_id, created_at, "
            "session_id) VALUES (?,?,?,?,?)",
            ("user", content, tid, now, identity._session_id),
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
