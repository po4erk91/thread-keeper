"""v1→v2 migration: content-storing dialog_fts → external-content."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Frozen v1 DDL (historical fact — do not "fix" to match current SCHEMA).
_V1_DIALOG_MESSAGES = """
CREATE TABLE dialog_messages (
    uuid         TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    project      TEXT,
    session_id   TEXT,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    model        TEXT,
    created_at   INTEGER NOT NULL,
    embedding    BLOB,
    embed_backend TEXT
);
"""
_V1_DIALOG_FTS = """
CREATE VIRTUAL TABLE dialog_fts USING fts5(
    uuid UNINDEXED,
    content
);
"""

_RAW_TOKEN = "c" * 24  # body of a fake GitHub token, a single FTS token
_RAW_SECRET = "ghp_" + _RAW_TOKEN


def _seed_v1(db_path: Path, *, scrubbed_fts: bool = True) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_V1_DIALOG_MESSAGES + _V1_DIALOG_FTS)
    rows = [
        ("v1-plain-1", "ordinary aardvark message"),
        ("v1-plain-2", "another aardvark note about retention"),
        ("v1-secret", f"my token is {_RAW_SECRET} keep it safe"),
    ]
    for uuid, content in rows:
        conn.execute(
            "INSERT INTO dialog_messages "
            "(uuid, source, project, session_id, role, content, model, created_at) "
            "VALUES (?, 'pytest', 'proj', 'sess', 'user', ?, NULL, 1800000000)",
            (uuid, content),
        )
        fts_content = content
        if scrubbed_fts and uuid == "v1-secret":
            # v1 behavior: the FTS copy was scrubbed, dialog_messages kept raw
            fts_content = "my token is [REDACTED:GITHUB_TOKEN] keep it safe"
        conn.execute(
            "INSERT INTO dialog_fts (uuid, content) VALUES (?, ?)",
            (uuid, fts_content),
        )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


def _boot_db(monkeypatch, db_path: Path):
    """Import threadkeeper fresh against a pre-seeded DB file."""
    monkeypatch.setenv("THREADKEEPER_DB", str(db_path))
    # isolate from the user's real ~/.threadkeeper/.env (pydantic env_file
    # is resolved from THREADKEEPER_ENV_FILE at config import time)
    monkeypatch.setenv("THREADKEEPER_ENV_FILE", str(db_path.parent / "empty.env"))
    monkeypatch.setenv("THREADKEEPER_DISABLE_BG_DAEMONS", "1")
    monkeypatch.setenv("THREADKEEPER_INGEST_INTERVAL_S", "0")
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper import db as tk_db

    return tk_db


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


def test_migrates_v1_to_external_content(tmp_path, monkeypatch):
    db_path = tmp_path / "v1.sqlite"
    _seed_v1(db_path)
    tk_db = _boot_db(monkeypatch, db_path)
    conn = tk_db.get_db()

    assert conn.execute("PRAGMA user_version").fetchone()[0] == (
        tk_db.CURRENT_SCHEMA_VERSION
    )
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dialog_fts'"
    ).fetchone()[0]
    assert "content='dialog_messages'" in ddl
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='dialog_fts_content'"
    ).fetchone() is None
    # search parity: same rows still match
    assert set(_match_uuids(conn, "aardvark")) == {"v1-plain-1", "v1-plain-2"}
    # all three rows are indexed — count docsize (one row per rowid actually
    # indexed); COUNT(*) on dialog_fts itself proxies to dialog_messages
    assert conn.execute(
        "SELECT COUNT(*) FROM dialog_fts_docsize"
    ).fetchone()[0] == 3
    conn.close()


def test_migration_scrubs_legacy_secret_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "v1.sqlite"
    _seed_v1(db_path)
    monkeypatch.setenv("THREADKEEPER_REDACT_DIALOG_SECRETS", "1")
    tk_db = _boot_db(monkeypatch, db_path)
    conn = tk_db.get_db()

    content = conn.execute(
        "SELECT content FROM dialog_messages WHERE uuid='v1-secret'"
    ).fetchone()[0]
    assert _RAW_SECRET not in content
    assert "[REDACTED:GITHUB_TOKEN]" in content
    # v1 kept raw secrets out of the FTS index; v2 must preserve that
    assert _match_uuids(conn, _RAW_TOKEN) == []
    assert _match_uuids(conn, "REDACTED") == ["v1-secret"]
    conn.close()


def test_migration_scrub_respects_disable_knob(tmp_path, monkeypatch):
    db_path = tmp_path / "v1.sqlite"
    _seed_v1(db_path, scrubbed_fts=False)
    monkeypatch.setenv("THREADKEEPER_REDACT_DIALOG_SECRETS", "0")
    tk_db = _boot_db(monkeypatch, db_path)
    conn = tk_db.get_db()

    content = conn.execute(
        "SELECT content FROM dialog_messages WHERE uuid='v1-secret'"
    ).fetchone()[0]
    assert _RAW_SECRET in content  # untouched when redaction is off
    conn.close()


def test_migration_rerun_is_noop(tmp_path, monkeypatch):
    db_path = tmp_path / "v1.sqlite"
    _seed_v1(db_path)
    tk_db = _boot_db(monkeypatch, db_path)
    conn = tk_db.get_db()
    counts1 = (
        conn.execute("SELECT COUNT(*) FROM dialog_messages").fetchone()[0],
        # docsize = real index size (COUNT(*) on dialog_fts proxies to
        # dialog_messages, so it can't detect a rebuild regression)
        conn.execute("SELECT COUNT(*) FROM dialog_fts_docsize").fetchone()[0],
    )
    conn.close()

    conn2 = tk_db.get_db()  # second connect: current version → fast path
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == (
        tk_db.CURRENT_SCHEMA_VERSION
    )
    counts2 = (
        conn2.execute("SELECT COUNT(*) FROM dialog_messages").fetchone()[0],
        conn2.execute("SELECT COUNT(*) FROM dialog_fts_docsize").fetchone()[0],
    )
    assert counts1 == counts2 == (3, 3)
    conn2.close()


def test_pre_versioning_db_with_old_fts_migrates(tmp_path, monkeypatch):
    """user_version=0 DB that already HAS tables (pre-versioning install):
    the shape-driven drop must still convert dialog_fts."""
    db_path = tmp_path / "v0.sqlite"
    _seed_v1(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    tk_db = _boot_db(monkeypatch, db_path)
    conn = tk_db.get_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == (
        tk_db.CURRENT_SCHEMA_VERSION
    )
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dialog_fts'"
    ).fetchone()[0]
    assert "content='dialog_messages'" in ddl
    assert set(_match_uuids(conn, "aardvark")) == {"v1-plain-1", "v1-plain-2"}
    conn.close()


def test_ensure_schema_waits_for_concurrent_migration(tmp_path, monkeypatch):
    """While another process holds the write lock mid-migration, get_db()'s
    schema check must wait and succeed once user_version reaches CURRENT —
    not die on the busy timeout."""
    import threading
    import time as _time

    db_path = tmp_path / "wait.sqlite"
    _seed_v1(db_path)
    tk_db = _boot_db(monkeypatch, db_path)

    # simulate the migrating process: hold BEGIN IMMEDIATE
    holder = sqlite3.connect(str(db_path), timeout=1.0)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")

    # check_same_thread=False: this connection is created here (main/test
    # thread) but exercised inside the background thread below, simulating
    # a concurrent get_db() caller on another connection.
    waiter_conn = sqlite3.connect(
        str(db_path), timeout=1.0, check_same_thread=False
    )
    waiter_conn.execute("PRAGMA busy_timeout=200")
    waiter_conn.row_factory = sqlite3.Row
    errors: list[BaseException] = []

    def _wait():
        try:
            tk_db._ensure_schema(waiter_conn, wait_s=30.0)
        except BaseException as e:  # noqa: BLE001 — surface into the test
            errors.append(e)

    t = threading.Thread(target=_wait, daemon=True)
    t.start()
    _time.sleep(1.0)
    assert t.is_alive()  # still waiting, not crashed

    # the "migrating" process finishes: version → CURRENT, lock released
    holder.execute(f"PRAGMA user_version = {tk_db.CURRENT_SCHEMA_VERSION}")
    holder.commit()
    holder.close()

    t.join(timeout=15.0)
    assert not t.is_alive()
    assert errors == []
    waiter_conn.close()
