"""A failed write must roll back instead of leaving its transaction open.

Python's sqlite3 opens a transaction before the first DML. When that DML or
the commit fails with "database is locked", the connection stays inside the
transaction (and its read snapshot) until somebody rolls back or closes it,
so long-lived daemon connections kept stale transactions open.
"""
from __future__ import annotations

import sqlite3


class _LockedConn:
    """Accepts reads, fails every commit, records rollbacks."""

    def __init__(self, fail_on: str = "commit"):
        self.fail_on = fail_on
        self.rolled_back = 0

    def execute(self, sql, params=()):
        if self.fail_on == "execute":
            raise sqlite3.OperationalError("database is locked")
        return _Cursor()

    def commit(self):
        raise sqlite3.OperationalError("database is locked")

    def rollback(self):
        self.rolled_back += 1


class _Cursor:
    rowcount = 1

    def fetchone(self):
        return None


def test_claim_pass_rolls_back_a_failed_claim(fresh_mp):
    from threadkeeper import daemon_state
    conn = _LockedConn()
    assert daemon_state.claim_pass("probe", 60, scheduled=True, conn=conn) is True
    assert conn.rolled_back == 1


def test_notification_record_rolls_back_and_still_dispatches(fresh_mp, monkeypatch):
    from threadkeeper import notify
    shown = []
    monkeypatch.setattr(notify, "_dispatch", lambda title, body: shown.append(title))
    conn = _LockedConn(fail_on="execute")
    notify._dispatch_recorded(conn, "event:1", ("Title", "Body"))
    assert conn.rolled_back == 1
    assert shown == ["Title"]


def test_notify_pass_record_rolls_back(fresh_mp):
    from threadkeeper import notify
    conn = _LockedConn()
    notify._record_notify_pass(conn, 1, 1, "ok")
    assert conn.rolled_back == 1


def test_ingest_pass_record_rolls_back(fresh_mp):
    from threadkeeper import ingest
    conn = _LockedConn()
    ingest._record_ingest_pass(conn, mode="recent", new_msgs=1, files_seen=1)
    assert conn.rolled_back == 1
