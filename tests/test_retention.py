"""Retention/compaction pass for high-volume SQLite tables."""
from __future__ import annotations

import time


def _retention_config(monkeypatch, retention, **overrides):
    names = (
        "DIALOG_RETENTION_DAYS",
        "TASK_RETENTION_DAYS",
        "SIGNAL_RETENTION_DAYS",
        "EVENTS_RETENTION_DAYS",
        "PROBE_RESULT_RETENTION_DAYS",
        "RETENTION_WAL_CHECKPOINT",
        "RETENTION_VACUUM_AFTER_ROWS",
    )
    defaults = {name: 0 for name in names}
    defaults["RETENTION_WAL_CHECKPOINT"] = False
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setattr(retention, name, value)


def _insert_dialog(conn, uuid: str, created_at: int) -> None:
    conn.execute(
        "INSERT INTO dialog_messages "
        "(uuid, source, project, session_id, role, content, model, created_at) "
        "VALUES (?, 'pytest', 'proj', 'sess', 'user', ?, NULL, ?)",
        (uuid, f"content {uuid}", created_at),
    )
    conn.execute(
        "INSERT INTO dialog_fts (uuid, content) VALUES (?, ?)",
        (uuid, f"content {uuid}"),
    )


def _maybe_vec_upsert(conn, uuid: str) -> bool:
    from threadkeeper import db

    if not db.vec_available():
        return False
    try:
        import sqlite_vec  # type: ignore
        from threadkeeper.config import EMBED_DIM
        from threadkeeper.embeddings import _vec_upsert_dialog

        emb = sqlite_vec.serialize_float32([0.01] * EMBED_DIM)
        _vec_upsert_dialog(conn, uuid, emb)
    except Exception:
        return False
    return True


def _count(conn, table: str, where: str = "1=1", params: tuple = ()) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()
    return int(row[0] or 0)


def test_retention_disabled_by_default_keeps_rows(fresh_mp, monkeypatch):
    from threadkeeper import retention

    conn = fresh_mp["db"].get_db()
    old = int(time.time()) - 100 * 86400
    _insert_dialog(conn, "old-default", old)
    conn.commit()

    _retention_config(monkeypatch, retention)
    out = retention.run_retention_pass(force=True)

    assert "total=0" in out, out
    assert _count(conn, "dialog_messages", "uuid='old-default'") == 1
    assert _count(conn, "dialog_fts", "uuid='old-default'") == 1


def test_dialog_retention_prunes_fts_and_vec_mirrors(fresh_mp, monkeypatch):
    from threadkeeper import retention

    conn = fresh_mp["db"].get_db()
    now = int(time.time())
    old = now - 100 * 86400
    _insert_dialog(conn, "old-a", old)
    _insert_dialog(conn, "old-b", old)
    _insert_dialog(conn, "new-a", now)
    vec_results = [_maybe_vec_upsert(conn, u) for u in ("old-a", "old-b", "new-a")]
    vec_checked = any(vec_results)
    conn.commit()

    _retention_config(monkeypatch, retention, DIALOG_RETENTION_DAYS=30)
    out = retention.run_retention_pass(force=True)

    assert "dialog=2" in out, out
    assert _count(conn, "dialog_messages") == 1
    assert _count(conn, "dialog_messages", "uuid='new-a'") == 1
    assert _count(conn, "dialog_fts") == 1
    assert _count(conn, "dialog_fts", "uuid='new-a'") == 1
    if vec_checked:
        assert _count(conn, "dialog_vec_map") == 1
        assert _count(conn, "dialog_vec_map", "uuid='new-a'") == 1
        orphan = conn.execute(
            "SELECT COUNT(*) FROM dialog_vec v "
            "LEFT JOIN dialog_vec_map m ON m.rowid = v.rowid "
            "WHERE m.rowid IS NULL"
        ).fetchone()[0]
        assert orphan == 0

    again = retention.run_retention_pass(force=True)
    assert "total=0" in again, again
    assert _count(conn, "dialog_messages") == 1
    assert _count(conn, "dialog_fts") == 1


def test_tasks_signals_and_events_retention(fresh_mp, monkeypatch):
    from threadkeeper import retention

    conn = fresh_mp["db"].get_db()
    now = int(time.time())
    old = now - 90 * 86400
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, ended_at) "
        "VALUES ('old_done', 0, '/tmp', 'p', ?, ?)",
        (old - 10, old),
    )
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, ended_at) "
        "VALUES ('new_done', 0, '/tmp', 'p', ?, ?)",
        (now - 10, now),
    )
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, ended_at) "
        "VALUES ('old_running', 0, '/tmp', 'p', ?, NULL)",
        (old,),
    )
    conn.execute(
        "INSERT INTO signals (from_cid, to_cid, kind, content, created_at, read_at) "
        "VALUES ('a', NULL, 'broadcast', 'old read', ?, ?)",
        (old, old + 1),
    )
    conn.execute(
        "INSERT INTO signals (from_cid, to_cid, kind, content, created_at, read_at) "
        "VALUES ('a', NULL, 'broadcast', 'old unread', ?, NULL)",
        (old,),
    )
    conn.execute(
        "INSERT INTO signals (from_cid, to_cid, kind, content, created_at, read_at) "
        "VALUES ('a', 'b', 'search_request', 'old request', ?, NULL)",
        (old,),
    )
    conn.execute(
        "INSERT INTO signals (from_cid, to_cid, kind, content, created_at, read_at) "
        "VALUES ('a', NULL, 'broadcast', 'new read', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('s', 'old_event', '', '', ?)",
        (old,),
    )
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('s', 'new_event', '', '', ?)",
        (now,),
    )
    conn.commit()

    _retention_config(
        monkeypatch,
        retention,
        TASK_RETENTION_DAYS=30,
        SIGNAL_RETENTION_DAYS=30,
        EVENTS_RETENTION_DAYS=30,
    )
    out = retention.run_retention_pass(force=True)

    assert "tasks=1" in out, out
    assert "signals=2" in out, out
    assert "events=1" in out, out
    assert _count(conn, "tasks", "id='old_done'") == 0
    assert _count(conn, "tasks", "id='new_done'") == 1
    assert _count(conn, "tasks", "id='old_running'") == 1
    assert _count(conn, "signals", "content='old read'") == 0
    assert _count(conn, "signals", "content='old request'") == 0
    assert _count(conn, "signals", "content='old unread'") == 1
    assert _count(conn, "signals", "content='new read'") == 1
    assert _count(conn, "events", "kind='old_event'") == 0
    assert _count(conn, "events", "kind='new_event'") == 1


def test_prune_then_vacuum_checkpoint_round_trip(fresh_mp, monkeypatch):
    from threadkeeper import retention

    conn = fresh_mp["db"].get_db()
    now = int(time.time())
    old = now - 90 * 86400
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, ended_at) "
        "VALUES ('old_for_maintenance', 0, '/tmp', 'p', ?, ?)",
        (old - 10, old),
    )
    conn.commit()

    _retention_config(
        monkeypatch,
        retention,
        TASK_RETENTION_DAYS=30,
        RETENTION_WAL_CHECKPOINT=True,
        RETENTION_VACUUM_AFTER_ROWS=1,
    )
    out = retention.run_retention_pass(force=True)

    assert "tasks=1" in out, out
    assert "vacuum=ok" in out, out
    assert "wal_checkpoint=ok" in out, out
    conn.execute(
        "INSERT INTO notes (content, kind, created_at) VALUES ('after prune', 'move', ?)",
        (now,),
    )
    conn.commit()
    assert _count(conn, "notes", "content='after prune'") == 1
