"""dialog_fts v2: external-content FTS5 shape, trigger sync, rebuild backfill."""
from __future__ import annotations


def _insert_msg(conn, uuid: str, content: str) -> None:
    conn.execute(
        "INSERT INTO dialog_messages "
        "(uuid, source, project, session_id, role, content, model, created_at) "
        "VALUES (?, 'pytest', 'proj', 'sess', 'user', ?, NULL, 1800000000)",
        (uuid, content),
    )


def _match_uuids(conn, term: str) -> list[str]:
    rows = conn.execute(
        "SELECT d.uuid FROM dialog_fts f "
        "JOIN dialog_messages d ON d.rowid = f.rowid "
        "WHERE dialog_fts MATCH ? ORDER BY rank",
        (term,),
    ).fetchall()
    return [r["uuid"] if hasattr(r, "keys") else r[0] for r in rows]


def test_fresh_schema_is_external_content(fresh_mp):
    conn = fresh_mp["db"].get_db()
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dialog_fts'"
    ).fetchone()[0]
    assert "content='dialog_messages'" in ddl
    assert "content_rowid='rowid'" in ddl
    # content-storing shadow copy must not exist on a fresh install
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='dialog_fts_content'"
    ).fetchone() is None
    triggers = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'dialog_fts_%'"
        ).fetchall()
    }
    assert triggers == {"dialog_fts_ai", "dialog_fts_ad", "dialog_fts_au"}


def test_fresh_db_starts_at_v2(fresh_mp):
    conn = fresh_mp["db"].get_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_trigger_insert_makes_row_searchable(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _insert_msg(conn, "m-ins", "zebra crossing procedure")
    conn.commit()
    assert _match_uuids(conn, "zebra") == ["m-ins"]


def test_trigger_update_reindexes(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _insert_msg(conn, "m-upd", "original giraffe text")
    conn.execute(
        "UPDATE dialog_messages SET content='replacement kangaroo text' "
        "WHERE uuid='m-upd'"
    )
    conn.commit()
    assert _match_uuids(conn, "giraffe") == []
    assert _match_uuids(conn, "kangaroo") == ["m-upd"]


def test_trigger_delete_removes_from_index(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _insert_msg(conn, "m-del", "ephemeral walrus entry")
    conn.execute("DELETE FROM dialog_messages WHERE uuid='m-del'")
    conn.commit()
    assert _match_uuids(conn, "walrus") == []
    assert conn.execute("SELECT COUNT(*) FROM dialog_fts").fetchone()[0] == 0


def test_backfill_rebuilds_empty_index(fresh_mp):
    from threadkeeper import ingest

    conn = fresh_mp["db"].get_db()
    for i in range(8):
        _insert_msg(conn, f"m-bf-{i}", f"backfill payload number{i} common")
    # wipe the index (rows stay in dialog_messages) — simulates a restored
    # DB / failed rebuild; counts now diverge by > 5
    conn.execute("INSERT INTO dialog_fts(dialog_fts) VALUES('delete-all')")
    conn.commit()
    assert _match_uuids(conn, "common") == []

    ingest._backfill_dialog_fts_if_empty(conn)

    assert len(_match_uuids(conn, "common")) == 8
    row = conn.execute(
        "SELECT value FROM style WHERE key='fts_backfilled'"
    ).fetchone()
    assert row is not None and row[0] == "8"


def test_backfill_noop_when_in_sync(fresh_mp):
    from threadkeeper import ingest

    conn = fresh_mp["db"].get_db()
    _insert_msg(conn, "m-sync", "already indexed muskox")
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM dialog_fts").fetchone()[0]
    ingest._backfill_dialog_fts_if_empty(conn)
    after = conn.execute("SELECT COUNT(*) FROM dialog_fts").fetchone()[0]
    assert before == after == 1
