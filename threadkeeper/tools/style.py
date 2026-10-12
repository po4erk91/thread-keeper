"""Stylistic running rules and verbatim user quotes."""

import sqlite3
import time
from .._mcp import write_tool
from ..db import run_write
from .. import identity
from ..identity import _emit
from ..config import WRITE_ORIGIN
from ..authority import authority_for_origin, record_origin_root


@write_tool()
def verbatim_user(content: str, thread_id: str = "") -> str:
    """Capture a user quote worth surfacing in future briefs. Use when the user's
    exact phrasing matters (sharp reframes, decisions, pushback)."""
    identity.ensure_session_started()
    if authority_for_origin(WRITE_ORIGIN) is None:
        return "ERR authority_unknown_origin"
    now = int(time.time())
    tid = thread_id.strip() or None

    def _write(conn: sqlite3.Connection) -> str:
        cur = conn.execute(
            "INSERT INTO verbatim (speaker, content, thread_id, created_at, "
            "session_id) VALUES (?,?,?,?,?)",
            ("user", content, tid, now, identity._session_id),
        )
        if not record_origin_root(
            conn, "verbatim", str(cur.lastrowid), write_origin=WRITE_ORIGIN,
            principal=identity._session_id or "unknown-principal", channel="mcp:verbatim",
        ):
            raise ValueError("authority_stamp_failed")
        _emit(conn, "verbatim_user", target=tid, summary=content)
        return "ok"

    try:
        return run_write("verbatim-user", _write)
    except ValueError as exc:
        if str(exc).startswith("authority_"):
            return f"ERR {exc}"
        raise


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
