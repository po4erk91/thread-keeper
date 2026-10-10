"""Leaked write transactions on legacy get_db() connections (#293).

A get_db() connection that runs an INSERT and never commits holds SQLite's
single writer lock for every process. These tests pin the guard that names
such a holder, and the run_write retry that must survive a lock hit while a
connection is still being set up.
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

HERE = "test_db_leak_guard.py"


def _mine(stats: dict) -> list[dict]:
    return [h for h in stats["write_holders"] if HERE in h["site"]]


def _pending_write(db):
    conn = db.get_db()
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('s-leak', 'leak_probe', NULL, '', 1)"
    )
    assert conn.in_transaction
    return conn


def test_legacy_connections_are_counted_until_closed(fresh_mp):
    db = fresh_mp["db"]
    before = db.legacy_connection_stats()["open"]

    conn = db.get_db()
    assert db.legacy_connection_stats()["open"] == before + 1

    conn.close()
    assert db.legacy_connection_stats()["open"] == before


def test_write_holder_is_named_with_its_call_site_and_age(fresh_mp):
    db = fresh_mp["db"]
    conn = _pending_write(db)
    try:
        first = _mine(db.legacy_connection_stats(now=1_000.0))
        assert len(first) == 1
        assert "_pending_write" in first[0]["site"]
        assert first[0]["held_s"] == 0

        later = _mine(db.legacy_connection_stats(now=1_090.0))
        assert later[0]["held_s"] == 90

        conn.commit()
        assert _mine(db.legacy_connection_stats(now=1_100.0)) == []
    finally:
        conn.close()


def test_host_logs_a_write_held_across_heartbeats(fresh_mp, caplog):
    from threadkeeper import host

    db = fresh_mp["db"]
    caplog.set_level(logging.ERROR, logger="threadkeeper.host")
    conn = _pending_write(db)
    try:
        assert host._check_leaked_writes(now=5_000.0) == []  # first sighting
        stale = host._check_leaked_writes(now=5_000.0 + 61)
        assert any(HERE in h["site"] for h in stale)
        assert "leaked get_db() transaction" in caplog.text
        assert "_pending_write" in caplog.text
    finally:
        conn.rollback()
        conn.close()


def test_run_write_deadline_warning_names_the_in_process_holder(fresh_mp, caplog):
    db = fresh_mp["db"]
    caplog.set_level(logging.WARNING, logger="threadkeeper.db")
    conn = _pending_write(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.run_write(
                "leak-probe",
                lambda c: c.execute(
                    "INSERT INTO events (session_id, kind, created_at) "
                    "VALUES ('s-2', 'blocked', 2)"
                ),
                deadline_s=0.3,
            )
    finally:
        conn.rollback()
        conn.close()
    assert "SQLite write deadline exhausted op=leak-probe" in caplog.text
    assert "write_holders=1" in caplog.text
    assert "_pending_write" in caplog.text


def test_run_write_retries_a_lock_during_connection_setup(fresh_mp, monkeypatch):
    db = fresh_mp["db"]
    db.bootstrap_db()
    real_open = db._open_connection
    calls = {"n": 0}

    def flaky_open(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_open(**kwargs)

    monkeypatch.setattr(db, "_open_connection", flaky_open)

    assert db.run_write("setup-lock", lambda c: c.execute("SELECT 7").fetchone()[0]) == 7
    assert calls["n"] == 2


def test_failed_connection_setup_closes_the_connection(fresh_mp, monkeypatch):
    db = fresh_mp["db"]
    opened: list[sqlite3.Connection] = []

    class _FailingSetup(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened.append(self)

        def execute(self, sql, *args):
            if sql.startswith("PRAGMA synchronous"):
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *args)

    with pytest.raises(sqlite3.OperationalError):
        db._open_connection(factory=_FailingSetup)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")  # closed, not leaked
