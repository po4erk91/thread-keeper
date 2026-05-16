"""brief() skill_hint nudge — fires when a recently-closed thread is rich
enough to be worth materializing as a Claude skill under ~/.claude/skills/.

After complex tasks, the agent should turn distilled insights into
reusable skills, not let them sit only in notes. The nudge surfaces
the moment.

Trigger:
- thread state = 'closed', closed within last 24h
- ≥5 notes total AND ≥2 of kind 'insight' or 'move'
- no prior 'skill_materialized' event for the thread

Escalates after 3+ consecutive shows without materialization.
"""
from __future__ import annotations

import time


_FAKE_CID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _brief_text(pkg):
    return pkg["mcp"]._tool_manager._tools["brief"].fn()


def _close_rich_thread(pkg, n_total: int = 6, n_rich: int = 3,
                      thread_q: str = "rich closed work"):
    """Open a thread, add notes, close it. Returns thread id."""
    open_t = pkg["mcp"]._tool_manager._tools["open_thread"].fn
    note = pkg["mcp"]._tool_manager._tools["note"].fn
    close = pkg["mcp"]._tool_manager._tools["close_thread"].fn
    tid_raw = open_t(question=thread_q)
    # open_thread returns the id directly (string), confirmed by spawn_hint tests.
    tid = tid_raw if isinstance(tid_raw, str) else tid_raw.get("result", tid_raw)
    rich_kinds = ["insight", "move"]
    for i in range(n_total):
        kind = rich_kinds[i % 2] if i < n_rich else "open_q"
        note(thread_id=tid, content=f"note #{i}", kind=kind)
    close(thread_id=tid, outcome="finished rich thread")
    return tid


def test_no_hint_when_no_closed_threads(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    txt = _brief_text(pkg)
    assert "skill_hint" not in txt


def test_no_hint_when_closed_thread_is_thin(mp_with_cid):
    """A closed thread with fewer than 5 notes should not trigger the hint."""
    pkg = mp_with_cid(_FAKE_CID)
    _close_rich_thread(pkg, n_total=3, n_rich=2)
    txt = _brief_text(pkg)
    assert "skill_hint" not in txt


def test_no_hint_when_closed_thread_lacks_rich_kinds(mp_with_cid):
    """5+ notes but all open_q — not rich enough to warrant a skill."""
    pkg = mp_with_cid(_FAKE_CID)
    open_t = pkg["mcp"]._tool_manager._tools["open_thread"].fn
    note = pkg["mcp"]._tool_manager._tools["note"].fn
    close = pkg["mcp"]._tool_manager._tools["close_thread"].fn
    tid = open_t(question="lots of questions, no insights")
    for i in range(6):
        note(thread_id=tid, content=f"q #{i}", kind="open_q")
    close(thread_id=tid, outcome="dropped")
    txt = _brief_text(pkg)
    assert "skill_hint" not in txt


def test_hint_fires_on_rich_closed_thread(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _close_rich_thread(pkg, n_total=6, n_rich=4)
    txt = _brief_text(pkg)
    assert "skill_hint" in txt
    assert "n=6" in txt
    assert "rich=4" in txt
    # Imperative imperative phrasing — match the spawn_hint convention.
    assert "MATERIALIZE" in txt
    assert "skill-creator" in txt or "skills/" in txt


def test_hint_disappears_after_materialization(mp_with_cid):
    """Logging a 'skill_materialized' event for the thread suppresses the hint."""
    pkg = mp_with_cid(_FAKE_CID)
    tid = _close_rich_thread(pkg, n_total=6, n_rich=4)
    txt_pre = _brief_text(pkg)
    assert "skill_hint" in txt_pre

    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?,?,?,?,?)",
        (pkg["identity"]._session_id or "test", "skill_materialized",
         tid, "wrote ~/.claude/skills/foo/SKILL.md", now),
    )
    conn.commit()

    txt_post = _brief_text(pkg)
    assert "skill_hint" not in txt_post


def test_mark_skill_materialized_clears_hint_and_writes_note(mp_with_cid):
    """The MCP tool should silence skill_hint AND append a move-note linking
    the thread to the skill path."""
    pkg = mp_with_cid(_FAKE_CID)
    tid = _close_rich_thread(pkg, n_total=6, n_rich=4)
    assert "skill_hint" in _brief_text(pkg)

    mark = pkg["mcp"]._tool_manager._tools["mark_skill_materialized"].fn
    result = mark(thread_id=tid,
                  skill_path="/Users/dmytro/.claude/skills/foo/SKILL.md")
    assert result == "ok"

    # Hint cleared.
    assert "skill_hint" not in _brief_text(pkg)

    # Note was appended.
    conn = pkg["db"].get_db()
    rows = conn.execute(
        "SELECT content, kind FROM notes WHERE thread_id=? "
        "ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchall()
    assert rows, "no note recorded"
    assert rows[0]["kind"] == "move"
    assert "/Users/dmytro/.claude/skills/foo/SKILL.md" in rows[0]["content"]

    # Event row exists.
    ev = conn.execute(
        "SELECT summary FROM events WHERE kind='skill_materialized' "
        "AND target=?",
        (tid,),
    ).fetchone()
    assert ev is not None
    assert "foo" in ev["summary"]


def test_mark_skill_materialized_works_without_path(mp_with_cid):
    """Empty skill_path is allowed — used to silence the hint when the path
    isn't recorded for some reason."""
    pkg = mp_with_cid(_FAKE_CID)
    tid = _close_rich_thread(pkg, n_total=6, n_rich=4)
    mark = pkg["mcp"]._tool_manager._tools["mark_skill_materialized"].fn
    assert mark(thread_id=tid) == "ok"
    assert "skill_hint" not in _brief_text(pkg)


def test_mark_skill_materialized_rejects_unknown_thread(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    mark = pkg["mcp"]._tool_manager._tools["mark_skill_materialized"].fn
    result = mark(thread_id="T_nope", skill_path="/whatever")
    assert result.startswith("ERR thread_not_found")


def test_hint_ignores_old_closures(mp_with_cid):
    """Threads closed more than 24h ago should not trigger the hint —
    we only nudge on fresh learnings."""
    pkg = mp_with_cid(_FAKE_CID)
    tid = _close_rich_thread(pkg, n_total=6, n_rich=4)
    # Rewind the thread's last_touched_at to 2 days ago.
    conn = pkg["db"].get_db()
    old = int(time.time()) - 2 * 86400
    conn.execute(
        "UPDATE threads SET last_touched_at=? WHERE id=?", (old, tid),
    )
    conn.commit()
    txt = _brief_text(pkg)
    assert "skill_hint" not in txt


def test_hint_escalates_after_repeated_ignores(mp_with_cid):
    """3+ shows without an intervening materialization escalate the hint."""
    pkg = mp_with_cid(_FAKE_CID)
    _close_rich_thread(pkg, n_total=6, n_rich=4)
    # First three shows — third call sees 2 prior shows logged so no escalation yet.
    _brief_text(pkg)
    _brief_text(pkg)
    txt3 = _brief_text(pkg)
    assert "ignored=" not in txt3
    txt4 = _brief_text(pkg)
    assert "ignored=3x" in txt4 or "ignored=" in txt4
    assert "⚠️" in txt4
