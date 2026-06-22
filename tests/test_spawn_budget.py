"""Spawn-budget admission control and live RSS tracking.

We don't launch real claude processes in these tests — admission and
status logic exercises the budget module directly against a temp DB.
"""
from __future__ import annotations

import os
import time

import pytest


_FAKE_CID = "33334444-5555-6666-7777-888899990000"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _txt(res):
    """Text payload from a tool result (str or CallToolResult, #67)."""
    if isinstance(res, str):
        return res
    return "\n".join(
        c.text for c in res.content if getattr(c, "type", None) == "text"
    )


def _insert_task(pkg, task_id, rss_kb=None, ended=False):
    """Insert a fake task. Uses the test process's own pid so
    _refresh_tasks (alive() check) doesn't mark it ended on a brief()
    or status() call."""
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, ended_at, rss_kb, rss_updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (task_id, os.getpid(), _FAKE_CID, f"child-{task_id}", "/tmp",
         "test", now, (now if ended else None), rss_kb, now),
    )
    conn.commit()


def _insert_visible_task(pkg, task_id, cid, rss_kb=None, started_at=None):
    """Insert a visible (Terminal-launched) task: pid=0, spawned_cid=cid.
    `cwd` points at a non-existent project dir so _refresh_tasks's jsonl-idle
    path resolves to 'no_jsonl' (the row stays running until the TTL/RSS path
    decides), isolating the #64 budget behaviour under test."""
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, ended_at, rss_kb, rss_updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (task_id, 0, _FAKE_CID, cid, "/tmp", "visible test",
         (started_at if started_at is not None else now), None, rss_kb, now),
    )
    conn.commit()


# ─────────────────────────────────────────────────────────────────────
# estimate_child_rss_kb
# ─────────────────────────────────────────────────────────────────────

def test_estimate_slim_returns_slim_constant(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN_ESTIMATE_SLIM_MB", "500")
    pkg = mp_with_cid(_FAKE_CID)  # fresh import picks up env
    from threadkeeper.spawn_budget import estimate_child_rss_kb
    assert estimate_child_rss_kb(slim=True) == 500 * 1024


def test_estimate_full_returns_full_constant(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN_ESTIMATE_FULL_MB", "1500")
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper.spawn_budget import estimate_child_rss_kb
    assert estimate_child_rss_kb(slim=False) == 1500 * 1024


# ─────────────────────────────────────────────────────────────────────
# check_budget — admission control
# ─────────────────────────────────────────────────────────────────────

def test_check_budget_admits_when_under_cap(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "3072")
    pkg = mp_with_cid(_FAKE_CID)
    _insert_task(pkg, "tk_a", rss_kb=500 * 1024)
    _insert_task(pkg, "tk_b", rss_kb=400 * 1024)

    from threadkeeper.spawn_budget import check_budget
    conn = pkg["db"].get_db()
    ok, msg = check_budget(conn, 500 * 1024)
    assert ok is True
    assert "ok" in msg.lower()


def test_check_budget_refuses_when_over_cap(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "1500")
    pkg = mp_with_cid(_FAKE_CID)
    _insert_task(pkg, "tk_a", rss_kb=800 * 1024)
    _insert_task(pkg, "tk_b", rss_kb=600 * 1024)

    from threadkeeper.spawn_budget import check_budget
    conn = pkg["db"].get_db()
    ok, msg = check_budget(conn, 500 * 1024)  # 1400 + 500 > 1500
    assert ok is False
    assert "budget_exceeded" in msg
    assert "1500" in msg


def test_check_budget_ignores_ended_tasks(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "1500")
    pkg = mp_with_cid(_FAKE_CID)
    _insert_task(pkg, "tk_done", rss_kb=1400 * 1024, ended=True)

    from threadkeeper.spawn_budget import check_budget
    conn = pkg["db"].get_db()
    ok, msg = check_budget(conn, 1000 * 1024)
    assert ok is True


def test_check_budget_disabled_when_zero(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "0")
    pkg = mp_with_cid(_FAKE_CID)
    _insert_task(pkg, "tk_huge", rss_kb=99999 * 1024)

    from threadkeeper.spawn_budget import check_budget
    conn = pkg["db"].get_db()
    ok, msg = check_budget(conn, 50000 * 1024)
    assert ok is True
    assert "disabled" in msg


def test_check_budget_treats_null_rss_as_conservative_estimate(mp_with_cid, monkeypatch):
    """When a row has NULL rss_kb (daemon hasn't measured yet), the check
    must assume full-estimate as a placeholder — otherwise a spawn flood
    could squeeze past the cap before measurement catches up."""
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "2048")
    monkeypatch.setenv("THREADKEEPER_SPAWN_ESTIMATE_FULL_MB", "1500")
    pkg = mp_with_cid(_FAKE_CID)
    _insert_task(pkg, "tk_unmeasured", rss_kb=None)

    from threadkeeper.spawn_budget import check_budget
    conn = pkg["db"].get_db()
    # 1500 (unmeasured placeholder) + 700 new = 2200 > 2048 → refused
    ok, msg = check_budget(conn, 700 * 1024)
    assert ok is False


# ─────────────────────────────────────────────────────────────────────
# spawn_budget_status MCP tool
# ─────────────────────────────────────────────────────────────────────

def test_spawn_budget_status_reports_running_children(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "3072")
    pkg = mp_with_cid(_FAKE_CID)
    _insert_task(pkg, "tk_x", rss_kb=400 * 1024)
    _insert_task(pkg, "tk_y", rss_kb=500 * 1024)
    _insert_task(pkg, "tk_done", rss_kb=999 * 1024, ended=True)

    txt = _txt(_tool(pkg, "spawn_budget_status")())
    assert "budget=3072MB" in txt
    # used = 400 + 500 = 900 (ended task excluded)
    assert "used=900MB" in txt
    assert "tk_x" in txt
    assert "tk_y" in txt
    assert "tk_done" not in txt


def test_spawn_budget_status_when_disabled(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "0")
    pkg = mp_with_cid(_FAKE_CID)
    txt = _txt(_tool(pkg, "spawn_budget_status")())
    assert "budget=disabled" in txt


# ─────────────────────────────────────────────────────────────────────
# Visible (pid=0) child RSS attribution + TTL reaper (#64)
# ─────────────────────────────────────────────────────────────────────

def test_visible_child_rss_measured_via_cid(mp_with_cid, monkeypatch):
    """A visible (pid=0) child's real RSS is resolved from its session-id and
    reflected in spawn_budget_status, replacing the static estimate (#64)."""
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "3072")
    pkg = mp_with_cid(_FAKE_CID)
    # Admitted at the 1500MB full-estimate; real tree is 1300MB.
    _insert_visible_task(pkg, "tk_vis", cid="vis-cid-1234", rss_kb=1500 * 1024)

    import threadkeeper.spawn_budget as sb
    monkeypatch.setattr(
        sb, "_pid_for_cid",
        lambda cid: 4242 if cid == "vis-cid-1234" else None,
    )
    monkeypatch.setattr(
        sb, "measure_tree_rss_kb",
        lambda pid: 1300 * 1024 if pid == 4242 else None,
    )

    conn = pkg["db"].get_db()
    assert sb._refresh_all_running(conn) == 1

    txt = _txt(_tool(pkg, "spawn_budget_status")())
    assert "tk_vis" in txt
    assert "pid=vis" in txt
    assert "rss=1300MB" in txt  # real measurement, not the 1500 estimate


def test_visible_pid_lookup_matches_session_id_in_argv(mp_with_cid, monkeypatch):
    """_pid_for_cid finds the claude child by its --session-id arg in `ps`."""
    pkg = mp_with_cid(_FAKE_CID)
    import threadkeeper.spawn_budget as sb

    fake_ps = (
        "  111 /sbin/launchd\n"
        "  222 claude -p hello --session-id abc-123-cid --model sonnet\n"
        "  333 /usr/bin/python server.py\n"
    )

    class _R:
        stdout = fake_ps

    monkeypatch.setattr(sb.subprocess, "run", lambda *a, **k: _R())
    assert sb._pid_for_cid("abc-123-cid") == 222
    assert sb._pid_for_cid("not-present") is None
    assert sb._pid_for_cid("") is None


def test_visible_ttl_reaps_unresolved_row(mp_with_cid, monkeypatch):
    """A visible row whose cid never resolves to a live process is reaped past
    SPAWN_VISIBLE_TTL_S and stops counting against the budget (#64)."""
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "3072")
    monkeypatch.setenv("THREADKEEPER_SPAWN_VISIBLE_TTL_S", "1800")
    pkg = mp_with_cid(_FAKE_CID)
    old = int(time.time()) - 2000  # older than the 1800s TTL
    _insert_visible_task(
        pkg, "tk_stuck", cid="ghost-cid", rss_kb=1500 * 1024, started_at=old,
    )

    import threadkeeper.spawn_budget as sb
    monkeypatch.setattr(sb, "_pid_for_cid", lambda cid: None)  # never resolves

    conn = pkg["db"].get_db()
    sb._refresh_all_running(conn)

    row = conn.execute(
        "SELECT ended_at FROM tasks WHERE id='tk_stuck'"
    ).fetchone()
    assert row["ended_at"] is not None
    assert sb._running_tasks_rss(conn) == 0  # no longer pins the budget


def test_visible_within_ttl_not_reaped(mp_with_cid, monkeypatch):
    """A young visible row with no resolvable process is left running until
    the TTL elapses — the reaper must not close live work prematurely."""
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "3072")
    monkeypatch.setenv("THREADKEEPER_SPAWN_VISIBLE_TTL_S", "1800")
    pkg = mp_with_cid(_FAKE_CID)
    young = int(time.time()) - 30
    _insert_visible_task(
        pkg, "tk_young", cid="ghost-cid", rss_kb=1500 * 1024, started_at=young,
    )

    import threadkeeper.spawn_budget as sb
    monkeypatch.setattr(sb, "_pid_for_cid", lambda cid: None)

    conn = pkg["db"].get_db()
    sb._refresh_all_running(conn)

    row = conn.execute(
        "SELECT ended_at FROM tasks WHERE id='tk_young'"
    ).fetchone()
    assert row["ended_at"] is None  # still within TTL → keeps counting


# ─────────────────────────────────────────────────────────────────────
# spawn_budget_set MCP tool
# ─────────────────────────────────────────────────────────────────────

def test_spawn_budget_set_lowers_cap_at_runtime(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_SPAWN_BUDGET_MB", "3072")
    pkg = mp_with_cid(_FAKE_CID)
    _tool(pkg, "spawn_budget_set")(limit_mb=1000)

    from threadkeeper import config
    assert config.SPAWN_BUDGET_MB == 1000

    txt = _txt(_tool(pkg, "spawn_budget_status")())
    assert "budget=1000MB" in txt


def test_spawn_budget_set_zero_disables(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    r = _tool(pkg, "spawn_budget_set")(limit_mb=0)
    assert "DISABLED" in r
    from threadkeeper import config
    assert config.SPAWN_BUDGET_MB == 0


def test_spawn_budget_set_rejects_negative(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    r = _tool(pkg, "spawn_budget_set")(limit_mb=-1)
    assert r.startswith("ERR")
