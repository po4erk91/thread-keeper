from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from threadkeeper.backup import create_backup, restore_backup


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def test_vacuum_snapshot_is_clean_under_concurrent_writes(tmp_path):
    src = tmp_path / "live.sqlite"
    dst = tmp_path / "snapshot.sqlite"
    conn = _connect(src)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, body TEXT)")
    payload = "x" * 4096
    conn.executemany(
        "INSERT INTO facts (body) VALUES (?)",
        [(f"seed-{idx}-{payload}",) for idx in range(900)],
    )
    conn.execute("INSERT INTO facts (body) VALUES ('committed-before-snapshot')")
    assert Path(f"{src}-wal").exists()

    stop = threading.Event()
    ready = threading.Event()
    writes: list[int] = []
    errors: list[str] = []

    def writer() -> None:
        writer_conn = _connect(src)
        writer_conn.execute("PRAGMA journal_mode=WAL")
        ready.set()
        i = 0
        try:
            while not stop.is_set():
                try:
                    writer_conn.execute(
                        "INSERT INTO facts (body) VALUES (?)",
                        (f"writer-{i}",),
                    )
                    writes.append(i)
                    i += 1
                except sqlite3.OperationalError as exc:
                    errors.append(str(exc))
                time.sleep(0.001)
        finally:
            writer_conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    assert ready.wait(timeout=5)
    while not writes:
        time.sleep(0.001)

    try:
        result = create_backup(dst, source=src)
    finally:
        stop.set()
        thread.join(timeout=5)
        conn.close()

    assert result.integrity == "ok"
    assert writes
    assert not errors
    assert not Path(f"{dst}-wal").exists()
    assert not Path(f"{dst}-shm").exists()

    snap = sqlite3.connect(str(dst))
    try:
        assert snap.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        row = snap.execute(
            "SELECT COUNT(*) FROM facts WHERE body='committed-before-snapshot'"
        ).fetchone()
        assert row[0] == 1
    finally:
        snap.close()


def test_restore_replaces_store_and_removes_stale_sidecars(tmp_path):
    db_path = tmp_path / "db.sqlite"
    old = _connect(db_path)
    old.execute("PRAGMA journal_mode=WAL")
    old.execute("CREATE TABLE facts (body TEXT)")
    old.execute("INSERT INTO facts VALUES ('old')")
    old.close()
    Path(f"{db_path}-wal").write_bytes(b"stale wal")
    Path(f"{db_path}-shm").write_bytes(b"stale shm")

    backup = tmp_path / "backup.sqlite"
    src = _connect(backup)
    src.execute("CREATE TABLE facts (body TEXT)")
    src.execute("INSERT INTO facts VALUES ('restored')")
    src.close()

    result = restore_backup(backup, destination=db_path)

    assert result.integrity == "ok"
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()
    restored = sqlite3.connect(str(db_path))
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT body FROM facts").fetchone()[0] == "restored"
    finally:
        restored.close()


def test_backup_of_gappy_db_keeps_fts_consistent(fresh_mp, tmp_path):
    """SQLite's contract permits VACUUM INTO to renumber dialog_messages'
    implicit rowids while the external-content dialog_fts index is copied
    verbatim — MATCHes in the artifact would then map to the wrong rows,
    and integrity_check cannot see it. On current builds (3.51/3.53
    verified) VACUUM INTO preserves implicit rowids in practice, so
    create_backup's rebuild is defensive — and a gap-only test passes
    vacuously even with the rebuild removed. To stay deterministic on any
    build, desync the source index directly (the same index-write command
    forms the triggers use) and require the artifact to come out
    repaired."""
    import sqlite3 as _sqlite3

    from threadkeeper.backup import create_backup

    def match(conn, term):
        return [
            r[0]
            for r in conn.execute(
                "SELECT d.uuid FROM dialog_fts f "
                "JOIN dialog_messages d ON d.rowid = f.rowid "
                "WHERE dialog_fts MATCH ? ORDER BY rank",
                (term,),
            ).fetchall()
        ]

    conn = fresh_mp["db"].get_db()
    for uuid, content in [
        ("m-1", "first pelican entry"),
        ("m-2", "second toucan entry"),
        ("m-3", "third condor entry"),
    ]:
        conn.execute(
            "INSERT INTO dialog_messages "
            "(uuid, source, project, session_id, role, content, model, created_at) "
            "VALUES (?, 'pytest', 'proj', 'sess', 'user', ?, NULL, 1800000000)",
            (uuid, content),
        )
    # rowid gap: SQLite's contract permits VACUUM INTO to renumber m-2/m-3
    # in the artifact (preserved on the builds we tested)
    conn.execute("DELETE FROM dialog_messages WHERE uuid='m-1'")
    conn.commit()

    # Deterministic desync (this platform's VACUUM INTO preserves rowids,
    # so we simulate what SQLite's contract permits): remove m-2's posting,
    # then map m-2's terms onto m-3's rowid — the source index now returns
    # wrong rows. These direct index writes never touch dialog_messages, so
    # no trigger fires.
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
    assert match(conn, "toucan") == ["m-3"]
    conn.close()

    dest = tmp_path / "artifact.sqlite"
    create_backup(dest)

    bconn = _sqlite3.connect(str(dest))
    try:
        assert match(bconn, "toucan") == ["m-2"]
        assert match(bconn, "condor") == ["m-3"]
        assert match(bconn, "pelican") == []
    finally:
        bconn.close()
