"""validate_threads heuristic triage tests.

Covers the four categories (no_notes_old, shipped, dropped_open_q,
stale_idle) and the dry_run vs apply distinction. Uses fresh_mp from
conftest for full isolation, and back-dates timestamps directly in SQL
to simulate aged threads without sleep.
"""
from __future__ import annotations

import time


def _tools(fresh_mp):
    return fresh_mp["mcp"]._tool_manager._tools


def _backdate(conn, tid: str, opened_days_ago: int, touched_days_ago: int) -> None:
    now = int(time.time())
    conn.execute(
        "UPDATE threads SET opened_at=?, last_touched_at=? WHERE id=?",
        (now - opened_days_ago * 86400, now - touched_days_ago * 86400, tid),
    )
    conn.commit()


def _backdate_note(conn, tid: str, days_ago: int) -> None:
    """Back-date all notes on a thread, preserving their original ordering
    by offsetting each row by its rank * 1s so ORDER BY created_at DESC is stable."""
    now = int(time.time())
    rows = conn.execute(
        "SELECT id FROM notes WHERE thread_id=? ORDER BY id ASC", (tid,)
    ).fetchall()
    base = now - days_ago * 86400
    for offset, r in enumerate(rows):
        conn.execute(
            "UPDATE notes SET created_at=? WHERE id=?",
            (base + offset, r["id"]),
        )
    conn.commit()


def test_dry_run_does_not_mutate_state(fresh_mp):
    t = _tools(fresh_mp)
    conn = fresh_mp["db"].get_db()
    tid = t["open_thread"].fn(question="never advanced")
    _backdate(conn, tid, opened_days_ago=30, touched_days_ago=30)

    out = t["validate_threads"].fn(dry_run=True)
    assert "dry_run=True" in out
    assert tid in out

    state = conn.execute("SELECT state FROM threads WHERE id=?", (tid,)).fetchone()["state"]
    assert state == "active"


def test_no_notes_old_closes_abandoned_thread(fresh_mp):
    t = _tools(fresh_mp)
    conn = fresh_mp["db"].get_db()
    tid = t["open_thread"].fn(question="abandoned at birth")
    _backdate(conn, tid, opened_days_ago=14, touched_days_ago=14)

    out = t["validate_threads"].fn(dry_run=False)
    assert "applied" in out

    row = conn.execute("SELECT state, outcome FROM threads WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "closed"
    assert "never advanced" in (row["outcome"] or "")


def test_shipped_marker_closes_with_last_move_outcome(fresh_mp):
    t = _tools(fresh_mp)
    conn = fresh_mp["db"].get_db()
    tid = t["open_thread"].fn(question="bug fix work")
    t["note"].fn(thread_id=tid, content="root cause found in payouts.ts", kind="insight")
    t["note"].fn(thread_id=tid, content="fix shipped in commit abc123, tests passing", kind="move")
    # Settled for 5 days
    _backdate(conn, tid, opened_days_ago=10, touched_days_ago=5)
    _backdate_note(conn, tid, days_ago=5)

    out = t["validate_threads"].fn(dry_run=False)
    row = conn.execute("SELECT state, outcome FROM threads WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "closed"
    assert "shipped" in (row["outcome"] or "").lower() or "commit" in (row["outcome"] or "")


def test_dropped_open_q_closes_after_threshold(fresh_mp):
    t = _tools(fresh_mp)
    conn = fresh_mp["db"].get_db()
    tid = t["open_thread"].fn(question="research")
    t["note"].fn(thread_id=tid, content="should we evaluate library X?", kind="open_q")
    _backdate(conn, tid, opened_days_ago=30, touched_days_ago=20)
    _backdate_note(conn, tid, days_ago=20)

    out = t["validate_threads"].fn(dry_run=False)
    row = conn.execute("SELECT state, outcome FROM threads WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "closed"
    assert "drop" in (row["outcome"] or "").lower() or "open question" in (row["outcome"] or "").lower()


def test_stale_idle_demotes_to_idle_not_closed(fresh_mp):
    t = _tools(fresh_mp)
    conn = fresh_mp["db"].get_db()
    tid = t["open_thread"].fn(question="long-running research")
    t["note"].fn(thread_id=tid, content="exploring landscape", kind="insight")
    _backdate(conn, tid, opened_days_ago=60, touched_days_ago=45)
    _backdate_note(conn, tid, days_ago=45)

    t["validate_threads"].fn(dry_run=False)
    row = conn.execute("SELECT state FROM threads WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "idle"


def test_fresh_thread_is_kept(fresh_mp):
    t = _tools(fresh_mp)
    conn = fresh_mp["db"].get_db()
    tid = t["open_thread"].fn(question="just started")
    t["note"].fn(thread_id=tid, content="initial probe", kind="move")

    out = t["validate_threads"].fn(dry_run=False)
    row = conn.execute("SELECT state FROM threads WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "active"


def test_idle_threads_are_not_touched(fresh_mp):
    t = _tools(fresh_mp)
    conn = fresh_mp["db"].get_db()
    tid = t["open_thread"].fn(question="paused work")
    t["idle_thread"].fn(thread_id=tid)
    _backdate(conn, tid, opened_days_ago=60, touched_days_ago=45)

    t["validate_threads"].fn(dry_run=False)
    row = conn.execute("SELECT state FROM threads WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "idle"


def test_shipped_marker_russian(fresh_mp):
    t = _tools(fresh_mp)
    conn = fresh_mp["db"].get_db()
    tid = t["open_thread"].fn(question="русский маркер")
    t["note"].fn(thread_id=tid, content="починено в коммите abc, тесты пройдены", kind="move")
    _backdate(conn, tid, opened_days_ago=10, touched_days_ago=5)
    _backdate_note(conn, tid, days_ago=5)

    t["validate_threads"].fn(dry_run=False)
    row = conn.execute("SELECT state FROM threads WHERE id=?", (tid,)).fetchone()
    assert row["state"] == "closed"
