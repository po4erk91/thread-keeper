"""Reaping headless spawned tasks: capture return_code, set ended_at.

Regression: the headless subprocess.Popen handle was dropped at spawn time
and nothing ever called waitpid on it, so tasks.return_code stayed NULL for
every task and finished children lingered as 'running' zombie rows.
"""
from __future__ import annotations

import os
import time

import pytest


@pytest.fixture
def conn(fresh_mp):
    """Initialized temp DB connection via the standard conftest bootstrap."""
    return fresh_mp["db"].get_db()


def _seed_task(conn, task_id: str, pid: int) -> None:
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at) "
        "VALUES (?,?,?,?,?)",
        (task_id, pid, "/tmp", "x", int(time.time())),
    )
    conn.commit()


def _reap_until_ended(conn, task_id: str, timeout_s: float = 2.0):
    """Call the reaper repeatedly (as _refresh_tasks would) until the row
    ends or we time out — avoids a fork/exec race in the assertion."""
    import threadkeeper.tools.spawn as sp
    deadline = time.time() + timeout_s
    row = None
    while time.time() < deadline:
        sp._reap_finished_tasks(conn)
        row = conn.execute(
            "SELECT ended_at, return_code FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row and row["ended_at"] is not None:
            return row
        time.sleep(0.02)
    return row


def test_returncode_zero_recorded_for_clean_exit(conn):
    pid = os.fork()
    if pid == 0:  # child
        os._exit(0)
    _seed_task(conn, "tk_ok", pid)
    row = _reap_until_ended(conn, "tk_ok")
    assert row is not None
    assert row["ended_at"] is not None
    assert row["return_code"] == 0


def test_returncode_nonzero_recorded(conn):
    pid = os.fork()
    if pid == 0:  # child
        os._exit(3)
    _seed_task(conn, "tk_rc3", pid)
    row = _reap_until_ended(conn, "tk_rc3")
    assert row is not None
    assert row["return_code"] == 3


def test_non_child_dead_pid_ends_without_returncode(conn):
    """A pid that isn't our child and isn't alive should be closed out
    (ended_at set) but with return_code left NULL — the exit code is
    unknowable for a process we didn't spawn/can't wait on."""
    dead_pid = 2_147_483_646  # no process lives this high; not our child
    _seed_task(conn, "tk_dead", dead_pid)
    import threadkeeper.tools.spawn as sp
    sp._reap_finished_tasks(conn)
    row = conn.execute(
        "SELECT ended_at, return_code FROM tasks WHERE id=?", ("tk_dead",)
    ).fetchone()
    assert row["ended_at"] is not None
    assert row["return_code"] is None
