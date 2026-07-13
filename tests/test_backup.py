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
