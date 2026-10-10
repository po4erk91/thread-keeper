"""Read tools must not heartbeat-write on every invocation."""
from __future__ import annotations

import asyncio
import time

def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def test_repeated_read_tools_do_not_touch_presence(fresh_mp, monkeypatch):
    identity = fresh_mp["identity"]
    db = fresh_mp["db"]
    conn = db.get_db()
    sid = identity._ensure_session(conn)
    conn.execute("UPDATE presence SET heartbeat_at=1 WHERE session_id=?", (sid,))
    conn.commit()
    conn.close()

    statements: list[str] = []
    original_open = db._open_connection

    def traced_open(*args, **kwargs):
        traced, vec_loaded = original_open(*args, **kwargs)
        traced.set_trace_callback(statements.append)
        return traced, vec_loaded

    monkeypatch.setattr(db, "_open_connection", traced_open)

    # Query scope is lean and the env suppresses the one-shot thread nudge, so
    # this brief has no legitimate "hint shown" event to record.
    monkeypatch.setenv("THREADKEEPER_BRIEF_NO_THREAD_NUDGE", "1")
    _tool(fresh_mp, "context")()
    _tool(fresh_mp, "search")(query="definitely absent token", k=3)
    _tool(fresh_mp, "dialog_search")(
        query="definitely absent token", k=3, mode="fts"
    )
    _tool(fresh_mp, "brief")(query="", scope="query")

    check = db.get_db()
    try:
        heartbeat = check.execute(
            "SELECT heartbeat_at FROM presence WHERE session_id=?", (sid,)
        ).fetchone()[0]
    finally:
        check.close()
    assert heartbeat == 1
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ")
    assert not [
        sql for sql in statements
        if sql.lstrip().upper().startswith(forbidden)
    ]
    assert sum("PRAGMA query_only=ON" in sql for sql in statements) >= 4


def test_read_surfaces_do_not_reap_or_link_cached_tasks(mp_with_cid, monkeypatch):
    """Dead/unlinked rows stay untouched until the explicit refresh action."""
    cid = "33334444-5555-6666-7777-888899990000"
    pkg = mp_with_cid(cid)
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, cwd, prompt, started_at, "
        "rss_kb, rss_updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("tk_dead_unlinked", 2_147_483_646, cid, "/tmp", "dead task", now - 60,
         1234, now - 60),
    )
    conn.commit()
    conn.close()

    import threadkeeper.host as host
    daemon_starts: list[bool] = []
    monkeypatch.setattr(host, "start_daemons", lambda: daemon_starts.append(True))

    _tool(pkg, "tasks")()
    _tool(pkg, "spawn_budget_status")()
    _tool(pkg, "agent_status")(refresh=True)
    _tool(pkg, "brief")()
    asyncio.run(pkg["mcp"].read_resource("memory://brief"))
    asyncio.run(pkg["mcp"].read_resource("memory://agent-status"))

    check = pkg["db"].get_db()
    try:
        task = check.execute(
            "SELECT ended_at, spawned_cid, rss_kb, rss_updated_at "
            "FROM tasks WHERE id='tk_dead_unlinked'"
        ).fetchone()
        assert tuple(task) == (None, None, 1234, now - 60)
        assert check.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM presence").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 0
    finally:
        check.close()
    assert daemon_starts == []

    assert _tool(pkg, "tasks_refresh")() == "ok freshness=refreshed"
    check = pkg["db"].get_db()
    try:
        ended_at = check.execute(
            "SELECT ended_at FROM tasks WHERE id='tk_dead_unlinked'"
        ).fetchone()[0]
    finally:
        check.close()
    assert ended_at is not None
