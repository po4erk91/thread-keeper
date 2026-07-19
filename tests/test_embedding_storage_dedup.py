"""Single-copy embedding storage: vec0 canonical, BLOB fallback only."""
from __future__ import annotations

import struct
import time

import pytest


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


@pytest.fixture()
def vec_pkg(fresh_mp):
    conn = fresh_mp["db"].get_db()
    conn.close()
    if not fresh_mp["db"].vec_available():
        pytest.skip("sqlite-vec extension not available")
    return fresh_mp


def _blob(dim: int, axis: int = 0) -> bytes:
    values = [0.0] * dim
    values[axis] = 1.0
    return struct.pack(f"{dim}f", *values)


def test_v4_dialog_fts_update_trigger_is_content_only(fresh_mp):
    conn = fresh_mp["db"].get_db()
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='dialog_fts_au'"
    ).fetchone()[0]
    assert "AFTER UPDATE OF content" in ddl
    conn.close()


def test_v3_migration_replaces_catch_all_dialog_trigger(fresh_mp):
    db = fresh_mp["db"]
    conn = db.get_db()
    conn.execute("DROP TRIGGER dialog_fts_au")
    conn.executescript(
        "CREATE TRIGGER dialog_fts_au AFTER UPDATE ON dialog_messages BEGIN "
        "INSERT INTO dialog_fts(dialog_fts,rowid,content) "
        "VALUES('delete',old.rowid,old.content); "
        "INSERT INTO dialog_fts(rowid,content) VALUES(new.rowid,new.content); "
        "END;"
    )
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()

    db._BOOTSTRAPPED = False
    migrated = db.get_db()
    ddl = migrated.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='dialog_fts_au'"
    ).fetchone()[0]
    assert "AFTER UPDATE OF content" in ddl
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 4
    migrated.close()


def test_embedding_dedup_tool_clears_only_vec_covered_blobs(vec_pkg):
    from threadkeeper import embeddings

    conn = vec_pkg["db"].get_db()
    dim = vec_pkg["config"].EMBED_DIM
    note_blob = _blob(dim, 0)
    dialog_blob = _blob(dim, 1)
    uncovered_blob = _blob(dim, 2)
    now = int(time.time())

    note = conn.execute(
        "INSERT INTO notes(content,kind,created_at,embedding,embed_backend) "
        "VALUES('covered note','insight',?,?,?)",
        (now, note_blob, embeddings.embedding_fingerprint()),
    ).lastrowid
    embeddings._vec_upsert_note(conn, note, note_blob)
    conn.execute("UPDATE notes SET embedding=? WHERE id=?", (note_blob, note))

    uncovered = conn.execute(
        "INSERT INTO notes(content,kind,created_at,embedding,embed_backend) "
        "VALUES('fallback note','insight',?,?,?)",
        (now, uncovered_blob, embeddings.embedding_fingerprint()),
    ).lastrowid

    uuid = "dedup-dialog"
    conn.execute(
        "INSERT INTO dialog_messages(uuid,source,project,session_id,role,content,"
        "model,created_at,embedding,embed_backend) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uuid, "pytest", "p", "s", "user", "deduplication toucan", None,
         now, dialog_blob, embeddings.embedding_fingerprint()),
    )
    embeddings._vec_upsert_dialog(conn, uuid, dialog_blob)
    conn.execute(
        "UPDATE dialog_messages SET embedding=? WHERE uuid=?",
        (dialog_blob, uuid),
    )
    conn.commit()

    dedup = _tool(vec_pkg, "db_deduplicate_embeddings")
    dry = dedup(dry_run=True)
    assert "dry_run embedding_dedup" in dry
    assert "rows=2" in dry
    assert f"bytes={len(note_blob) + len(dialog_blob)}" in dry
    assert "uncovered=1" in dry
    assert conn.execute(
        "SELECT embedding FROM notes WHERE id=?", (note,)
    ).fetchone()[0] is not None

    applied = dedup(dry_run=False)
    assert "ok embedding_dedup" in applied
    assert "rows=2" in applied
    assert conn.execute(
        "SELECT embedding FROM notes WHERE id=?", (note,)
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT embedding FROM notes WHERE id=?", (uncovered,)
    ).fetchone()[0] == uncovered_blob
    assert conn.execute(
        "SELECT embedding FROM dialog_messages WHERE uuid=?", (uuid,)
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT embedding FROM notes_vec WHERE id=?", (note,)
    ).fetchone()[0] == note_blob
    assert conn.execute(
        "SELECT v.embedding FROM dialog_vec v "
        "JOIN dialog_vec_map m ON m.rowid=v.rowid WHERE m.uuid=?", (uuid,)
    ).fetchone()[0] == dialog_blob
    assert conn.execute(
        "SELECT d.uuid FROM dialog_fts f JOIN dialog_messages d ON d.rowid=f.rowid "
        "WHERE dialog_fts MATCH 'toucan'"
    ).fetchone()[0] == uuid
    assert "rows=0" in dedup(dry_run=False)
    conn.close()


def test_sync_applying_guard_is_reentrant(fresh_mp):
    from threadkeeper.sync.capture import applying_guard

    conn = fresh_mp["db"].get_db()
    probe = (
        "SELECT count(*) FROM pragma_table_list "
        "WHERE schema='temp' AND name='_tk_sync_applying'"
    )
    with applying_guard(conn):
        assert conn.execute(probe).fetchone()[0] == 1
        with applying_guard(conn):
            assert conn.execute(probe).fetchone()[0] == 1
        assert conn.execute(probe).fetchone()[0] == 1
    assert conn.execute(probe).fetchone()[0] == 0
    conn.close()
