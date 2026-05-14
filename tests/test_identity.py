"""Regression tests for the `from ..identity import _session_id` snapshot bug.

Python's `from module import attr` binds the name to whatever the module
exports AT IMPORT TIME. If the source module rebinds the global later
(via `global _session_id; _session_id = "s_..."`), the importing module
keeps the original (None) reference forever.

Tools must always read identity through `identity._session_id` (attribute
access on the module), never via a local-scope import. This file pins
that contract.
"""
from __future__ import annotations

import re


def test_session_id_set_after_ensure_session(fresh_mp):
    identity = fresh_mp["identity"]
    db = fresh_mp["db"]
    assert identity._session_id is None
    conn = db.get_db()
    sid = identity._ensure_session(conn)
    assert sid.startswith("s_")
    assert identity._session_id == sid


def test_context_sees_live_session_id(fresh_mp):
    """context() must reflect the current session, not None.

    This is the exact regression from `name '_session_start' is not defined` /
    `sess=-` in production briefs.
    """
    mcp = fresh_mp["mcp"]
    out = mcp._tool_manager._tools["context"].fn()
    # Whatever happens, must be a non-empty string with sess= prefix populated
    assert isinstance(out, str)
    assert "sess=s_" in out, f"context didn't see live session: {out!r}"
    assert "sess=None" not in out
    # Must include the rest of the documented fields
    assert "sem=" in out
    assert "db=" in out
    assert "threads[" in out
    assert "now=" in out


def test_brief_ctx_line_carries_live_session_id(fresh_mp):
    """The first line of brief() reads `ctx sess=...`. If the snapshot bug
    is present, that prints `sess=-`. Pin to the live id."""
    mcp = fresh_mp["mcp"]
    out = mcp._tool_manager._tools["brief"].fn()
    first = out.split("\n", 1)[0]
    assert first.startswith("ctx sess=")
    assert "sess=-" not in first, f"brief showed snapshot None: {first}"
    assert "sess=s_" in first, f"brief didn't show live id: {first}"


def test_note_records_session_id(fresh_mp):
    """note() persists session_id alongside the note. If the snapshot bug
    is present, every note ends up with NULL session_id even though the
    session is alive."""
    mcp = fresh_mp["mcp"]
    db = fresh_mp["db"]
    open_thread = mcp._tool_manager._tools["open_thread"].fn
    note_fn = mcp._tool_manager._tools["note"].fn

    tid = open_thread(question="probe session id binding")
    assert re.match(r"^T[0-9a-f]{3,}$", tid)
    note_fn(thread_id=tid, content="testnote", kind="move")

    conn = db.get_db()
    row = conn.execute(
        "SELECT session_id FROM notes WHERE thread_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert row is not None
    assert row["session_id"] is not None
    assert row["session_id"].startswith("s_"), \
        f"note got stale snapshot: session_id={row['session_id']!r}"
