"""Thread-state-machine integration tests.

open_thread → note → close_thread end-to-end. Verifies brief() surfaces
the open thread and stops surfacing once closed.
"""
from __future__ import annotations


def _tools(fresh_mp):
    return fresh_mp["mcp"]._tool_manager._tools


def test_thread_lifecycle(fresh_mp):
    t = _tools(fresh_mp)
    tid = t["open_thread"].fn(question="lifecycle test")
    assert tid.startswith("T")

    out = t["note"].fn(thread_id=tid, content="moved A to B", kind="move")
    assert out.startswith("ok")

    out = t["note"].fn(thread_id=tid, content="reflexion", kind="insight")
    assert out.startswith("ok")

    out = t["close_thread"].fn(thread_id=tid, outcome="resolved via B")
    assert out == "ok"


def test_note_to_unknown_thread_returns_err(fresh_mp):
    t = _tools(fresh_mp)
    out = t["note"].fn(thread_id="Tnope", content="x", kind="move")
    assert out.startswith("ERR")
    assert "thread_not_found" in out


def test_brief_includes_open_thread(fresh_mp):
    t = _tools(fresh_mp)
    tid = t["open_thread"].fn(question="brief should surface this")
    t["note"].fn(thread_id=tid, content="step one", kind="move")

    brief = t["brief"].fn()
    assert isinstance(brief, str)
    # The open thread must appear in the brief somewhere — either by
    # last_move text or by the explicit thread question.
    assert "brief should surface this" in brief or tid in brief


def test_brief_drops_closed_thread_from_open_section(fresh_mp):
    t = _tools(fresh_mp)
    tid = t["open_thread"].fn(question="closed thread should leave open list")
    t["close_thread"].fn(thread_id=tid, outcome="done")

    brief = t["brief"].fn()
    # In the "open" section we should not find this thread's line. brief format
    # mentions closed threads in `closed_recent` section, not `open`.
    open_section = brief.split("closed_recent")[0]
    assert "closed thread should leave open list" not in open_section


def test_idle_then_note_revives(fresh_mp):
    t = _tools(fresh_mp)
    db = fresh_mp["db"]
    tid = t["open_thread"].fn(question="idle revival check")
    t["idle_thread"].fn(thread_id=tid)

    conn = db.get_db()
    state = conn.execute("SELECT state FROM threads WHERE id=?", (tid,)).fetchone()["state"]
    assert state == "idle"

    t["note"].fn(thread_id=tid, content="back at it", kind="move")
    state = conn.execute("SELECT state FROM threads WHERE id=?", (tid,)).fetchone()["state"]
    assert state == "active"
