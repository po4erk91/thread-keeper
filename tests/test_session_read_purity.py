"""Read tools must not heartbeat-write on every invocation."""
from __future__ import annotations


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
