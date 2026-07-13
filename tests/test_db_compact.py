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
    """THE regression guard for the external-content trap: VACUUM renumbers
    dialog_messages' implicit rowids; without the rebuild the index maps
    MATCHes to wrong/missing rows. After db_compact, every row must still
    resolve to its correct uuid."""
    conn = fresh_mp["db"].get_db()
    _insert_msg(conn, "m-1", "first pelican entry")
    _insert_msg(conn, "m-2", "second toucan entry")
    _insert_msg(conn, "m-3", "third condor entry")
    # deleting the lowest rowid creates the gap VACUUM will compact away,
    # shifting m-2/m-3 onto new rowids
    conn.execute("DELETE FROM dialog_messages WHERE uuid='m-1'")
    conn.commit()
    conn.close()

    out = _tool(fresh_mp, "db_compact")()
    assert out.startswith("ok"), out

    conn = fresh_mp["db"].get_db()
    assert _match_uuids(conn, "toucan") == ["m-2"]
    assert _match_uuids(conn, "condor") == ["m-3"]
    assert _match_uuids(conn, "pelican") == []
    assert conn.execute("SELECT COUNT(*) FROM dialog_fts").fetchone()[0] == 2
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_db_compact_single_flight(fresh_mp):
    from threadkeeper.helpers import single_flight_lock

    with single_flight_lock("db-compact") as locked:
        assert locked
        out = _tool(fresh_mp, "db_compact")()
    assert "already running" in out
