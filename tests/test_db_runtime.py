"""Connection-role and short write-transaction contracts."""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest


def test_get_db_reuses_process_bootstrap(fresh_mp, monkeypatch):
    db = fresh_mp["db"]
    first = db.get_db()
    first.close()

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("ordinary connection repeated bootstrap work")

    monkeypatch.setattr(db, "_ensure_schema", _unexpected)
    monkeypatch.setattr(db, "_execute_startup_pragma", _unexpected)
    second = db.get_db()
    try:
        assert second.execute("SELECT 1").fetchone()[0] == 1
    finally:
        second.close()


def test_read_db_is_query_only_and_closes(fresh_mp):
    db = fresh_mp["db"]
    with db.read_db() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(
                "INSERT INTO style(key, value, updated_at) VALUES ('x','y',1)"
            )
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_run_write_retries_at_transaction_boundary(fresh_mp):
    db = fresh_mp["db"]
    seed = db.get_db()
    seed.close()

    holder = sqlite3.connect(str(fresh_mp["config"].DB_PATH), isolation_level=None)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")

    result: list[str] = []
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            out = db.run_write(
                "runtime-test",
                lambda conn: conn.execute(
                    "INSERT INTO style(key, value, updated_at) VALUES ('retry','ok',1)"
                ).rowcount,
                deadline_s=3.0,
            )
            result.append(str(out))
        except BaseException as exc:  # noqa: BLE001 — surface thread failure
            errors.append(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    time.sleep(0.4)  # at least one 250ms busy slice must expire
    assert thread.is_alive()
    holder.commit()
    holder.close()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert result == ["1"]
    conn = db.get_db()
    try:
        assert conn.execute(
            "SELECT value FROM style WHERE key='retry'"
        ).fetchone()[0] == "ok"
    finally:
        conn.close()


def test_run_write_rolls_back_callback_failure(fresh_mp):
    db = fresh_mp["db"]

    def _broken(conn):
        conn.execute(
            "INSERT INTO style(key, value, updated_at) VALUES ('broken','x',1)"
        )
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        db.run_write("broken-test", _broken)

    conn = db.get_db()
    try:
        assert conn.execute(
            "SELECT 1 FROM style WHERE key='broken'"
        ).fetchone() is None
    finally:
        conn.close()
