"""db_compact(): VACUUM + mandatory dialog_fts rebuild (opt-in reclaim)."""
from __future__ import annotations


def _tool(pkg, name: str):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _insert_msg(conn, uuid: str, content: str) -> None:
    conn.execute(
        "INSERT INTO dialog_messages "
        "(uuid, source, project, session_id, role, content, model, created_at) "
        "VALUES (?, 'pytest', 'proj', 'sess', 'user', ?, NULL, 1800000000)",
        (uuid, content),
    )


def _match_uuids(conn, term: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT d.uuid FROM dialog_fts f "
            "JOIN dialog_messages d ON d.rowid = f.rowid "
            "WHERE dialog_fts MATCH ? ORDER BY rank",
            (term,),
        ).fetchall()
    ]


def test_db_compact_registered(fresh_mp):
    assert "db_compact" in fresh_mp["mcp"]._tool_manager._tools


def test_db_compact_survives_rowid_renumbering(fresh_mp):
    """THE regression guard for the external-content trap: SQLite's contract
    permits VACUUM to renumber dialog_messages' implicit rowids, which would
    desync the index and map MATCHes to wrong/missing rows. On current
    builds (3.51/3.53 verified) VACUUM preserves implicit rowids in
    practice, so the rebuild is defensive — and a VACUUM-only test passes
    vacuously even with the rebuild removed. To stay deterministic on any
    build, desync the index directly (the same index-write command forms
    the triggers use) and require db_compact's rebuild to repair it: every
    MATCH must resolve to its correct uuid again."""
    conn = fresh_mp["db"].get_db()
    _insert_msg(conn, "m-1", "first pelican entry")
    _insert_msg(conn, "m-2", "second toucan entry")
    _insert_msg(conn, "m-3", "third condor entry")
    # rowid gap: SQLite's contract permits VACUUM to renumber m-2/m-3 into
    # it (preserved on the builds we tested)
    conn.execute("DELETE FROM dialog_messages WHERE uuid='m-1'")
    conn.commit()

    # Deterministic desync (this platform's VACUUM preserves rowids, so we
    # simulate what SQLite's contract permits): remove m-2's posting, then
    # map m-2's terms onto m-3's rowid — the index now returns wrong rows.
    # These direct index writes never touch dialog_messages, so no trigger
    # fires.
    r2 = conn.execute(
        "SELECT rowid FROM dialog_messages WHERE uuid='m-2'"
    ).fetchone()[0]
    r3 = conn.execute(
        "SELECT rowid FROM dialog_messages WHERE uuid='m-3'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO dialog_fts(dialog_fts, rowid, content) "
        "VALUES('delete', ?, 'second toucan entry')",
        (r2,),
    )
    conn.execute(
        "INSERT INTO dialog_fts(rowid, content) "
        "VALUES(?, 'second toucan entry')",
        (r3,),
    )
    conn.commit()
    # precondition: the desync is real — 'toucan' now maps to the WRONG row
    assert _match_uuids(conn, "toucan") == ["m-3"]
    conn.close()

    out = _tool(fresh_mp, "db_compact")()
    assert out.startswith("ok"), out

    conn = fresh_mp["db"].get_db()
    assert _match_uuids(conn, "toucan") == ["m-2"]
    assert _match_uuids(conn, "condor") == ["m-3"]
    assert _match_uuids(conn, "pelican") == []
    # docsize = real index size; COUNT(*) on the external-content dialog_fts
    # proxies to dialog_messages and would pass even with a broken rebuild
    assert conn.execute(
        "SELECT COUNT(*) FROM dialog_fts_docsize"
    ).fetchone()[0] == 2
    assert conn.execute("PRAGMA user_version").fetchone()[0] == \
        fresh_mp["db"].CURRENT_SCHEMA_VERSION


def test_db_compact_single_flight(fresh_mp):
    from threadkeeper.helpers import single_flight_lock

    with single_flight_lock("db-compact") as locked:
        assert locked
        out = _tool(fresh_mp, "db_compact")()
    assert "already running" in out
