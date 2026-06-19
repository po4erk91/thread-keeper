"""Orphan-detection logic for thread-keeper processes.

We mock the ps walk and pid-aliveness so tests don't depend on what's
actually running on the host.
"""
from __future__ import annotations

import os
import time

import pytest


_FAKE_CID = "44445555-6666-7777-8888-999900001111"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _txt(res):
    """Text payload from a tool result (str or CallToolResult, #67)."""
    if isinstance(res, str):
        return res
    return "\n".join(
        c.text for c in res.content if getattr(c, "type", None) == "text"
    )


# ─────────────────────────────────────────────────────────────────────
# classify() — the heart of the logic
# ─────────────────────────────────────────────────────────────────────

def test_classify_self_never_orphan(mp_with_cid):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import process_health
    p = {"pid": os.getpid(), "ppid": 1, "rss_kb": 1000, "etime": "1:00",
         "command": "..."}
    out = process_health.classify(p, process_health.get_db())
    assert out["is_self"] is True
    assert out["is_orphaned"] is False
    assert out["orphan_reason"] == "self"


def test_classify_parent_alive_not_orphan(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import process_health
    # Parent is THIS process (test runner) — definitely alive
    p = {"pid": 99999999, "ppid": os.getpid(), "rss_kb": 200_000,
         "etime": "10:00", "command": "threadkeeper.server"}
    out = process_health.classify(p, process_health.get_db())
    assert out["parent_alive"] is True
    assert out["is_orphaned"] is False
    assert "parent_alive" in out["orphan_reason"]


def test_classify_parent_dead_no_heartbeat_is_orphan(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import process_health
    # Parent ppid=1 → counts as no real parent
    p = {"pid": 88888887, "ppid": 1, "rss_kb": 300_000,
         "etime": "1:00:00", "command": "threadkeeper.server"}
    out = process_health.classify(p, process_health.get_db())
    assert out["parent_alive"] is False
    assert out["heartbeat_age_s"] is None  # no presence row for that pid
    assert out["is_orphaned"] is True
    assert "no_heartbeat" in out["orphan_reason"]


def test_classify_parent_dead_fresh_heartbeat_not_orphan(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import process_health
    # Seed a fresh presence row for a fake pid
    fake_pid = 88888888
    sid = f"s_{fake_pid}_abcd"
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO presence (session_id, client, started_at, heartbeat_at) "
        "VALUES (?, 'test', ?, ?)",
        (sid, now - 60, now - 10),  # heartbeat 10s ago = fresh
    )
    conn.commit()
    p = {"pid": fake_pid, "ppid": 1, "rss_kb": 300_000,
         "etime": "1:00", "command": "threadkeeper.server"}
    out = process_health.classify(p, process_health.get_db())
    assert out["parent_alive"] is False
    assert out["heartbeat_age_s"] is not None
    assert out["heartbeat_age_s"] < 60
    assert out["is_orphaned"] is False
    assert "heartbeat fresh" in out["orphan_reason"]


def test_classify_parent_dead_stale_heartbeat_is_orphan(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import process_health
    fake_pid = 88888889
    sid = f"s_{fake_pid}_efgh"
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Heartbeat 10 minutes ago > STALE_HEARTBEAT_S (5 min)
    conn.execute(
        "INSERT INTO presence (session_id, client, started_at, heartbeat_at) "
        "VALUES (?, 'test', ?, ?)",
        (sid, now - 3600, now - 600),
    )
    conn.commit()
    p = {"pid": fake_pid, "ppid": 1, "rss_kb": 300_000,
         "etime": "1:00:00", "command": "threadkeeper.server"}
    out = process_health.classify(p, process_health.get_db())
    assert out["parent_alive"] is False
    assert out["is_orphaned"] is True
    assert "heartbeat_age" in out["orphan_reason"]


# ─────────────────────────────────────────────────────────────────────
# cleanup() dry-run + apply
# ─────────────────────────────────────────────────────────────────────

def test_cleanup_dry_run_does_not_kill(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import process_health

    # Stub scan() to return a fake orphan with a fake pid that doesn't exist
    fake_orphan = {
        "pid": 77777771, "ppid": 1, "rss_kb": 250_000, "etime": "20:00",
        "command": "threadkeeper.server", "parent_alive": False,
        "heartbeat_age_s": 1000, "is_self": False, "is_orphaned": True,
        "orphan_reason": "parent_gone + heartbeat_age=1000s > 300s",
    }
    monkeypatch.setattr(process_health, "scan", lambda: [fake_orphan])

    killed_pids: list = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed_pids.append(pid))

    result = process_health.cleanup(dry_run=True)
    assert result["dry_run"] is True
    assert len(result["orphans"]) == 1
    assert result["killed"] == []
    # os.kill must NOT have been called in dry-run mode
    assert killed_pids == []


def test_cleanup_apply_sends_signal(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import process_health

    fake_orphan = {
        "pid": 77777772, "ppid": 1, "rss_kb": 250_000, "etime": "20:00",
        "command": "threadkeeper.server", "parent_alive": False,
        "heartbeat_age_s": 1000, "is_self": False, "is_orphaned": True,
        "orphan_reason": "parent_gone + heartbeat_age=1000s > 300s",
    }
    monkeypatch.setattr(process_health, "scan", lambda: [fake_orphan])

    killed_pids: list = []
    sigs: list = []
    def fake_kill(pid, sig):
        killed_pids.append(pid)
        sigs.append(sig)
    monkeypatch.setattr("os.kill", fake_kill)

    result = process_health.cleanup(dry_run=False, force=False)
    assert result["dry_run"] is False
    assert killed_pids == [77777772]
    import signal as _sig
    assert sigs == [_sig.SIGTERM]


def test_cleanup_force_uses_sigkill(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import process_health

    fake_orphan = {
        "pid": 77777773, "ppid": 1, "rss_kb": 100_000, "etime": "5:00",
        "command": "threadkeeper.server", "parent_alive": False,
        "heartbeat_age_s": 999, "is_self": False, "is_orphaned": True,
        "orphan_reason": "parent_gone + heartbeat_age=999s > 300s",
    }
    monkeypatch.setattr(process_health, "scan", lambda: [fake_orphan])

    sigs: list = []
    monkeypatch.setattr("os.kill", lambda pid, sig: sigs.append(sig))

    process_health.cleanup(dry_run=False, force=True)
    import signal as _sig
    assert sigs == [_sig.SIGKILL]


def test_cleanup_handles_already_dead_process(mp_with_cid, monkeypatch):
    """A pid that died between scan and kill should land in `failed`,
    not raise."""
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import process_health

    fake_orphan = {
        "pid": 77777774, "ppid": 1, "rss_kb": 100_000, "etime": "5:00",
        "command": "threadkeeper.server", "parent_alive": False,
        "heartbeat_age_s": 999, "is_self": False, "is_orphaned": True,
        "orphan_reason": "test",
    }
    monkeypatch.setattr(process_health, "scan", lambda: [fake_orphan])

    def raise_lookup(pid, sig):
        raise ProcessLookupError(f"no such pid {pid}")
    monkeypatch.setattr("os.kill", raise_lookup)

    result = process_health.cleanup(dry_run=False)
    assert result["killed"] == []
    assert len(result["failed"]) == 1
    assert result["failed"][0]["pid"] == 77777774


# ─────────────────────────────────────────────────────────────────────
# MCP tool surface
# ─────────────────────────────────────────────────────────────────────

def test_mp_health_tool_shows_table(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import process_health
    monkeypatch.setattr(process_health, "scan", lambda: [
        {"pid": 1001, "ppid": 1, "rss_kb": 300_000, "etime": "10:00",
         "command": "threadkeeper.server", "parent_alive": False,
         "heartbeat_age_s": 1000, "is_self": False, "is_orphaned": True,
         "orphan_reason": "parent_gone + heartbeat_age=1000s > 300s"},
        {"pid": 1002, "ppid": os.getpid(), "rss_kb": 200_000, "etime": "5:00",
         "command": "threadkeeper.server", "parent_alive": True,
         "heartbeat_age_s": 10, "is_self": False, "is_orphaned": False,
         "orphan_reason": "parent_alive"},
    ])
    txt = _txt(_tool(pkg, "mp_health")())
    assert "total=2" in txt
    assert "orphans=1" in txt
    assert "ORPHAN" in txt
    assert "live" in txt


def test_mp_cleanup_dry_run_default(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import process_health
    monkeypatch.setattr(process_health, "scan", lambda: [
        {"pid": 2001, "ppid": 1, "rss_kb": 250_000, "etime": "20:00",
         "command": "threadkeeper.server", "parent_alive": False,
         "heartbeat_age_s": 999, "is_self": False, "is_orphaned": True,
         "orphan_reason": "test"},
    ])
    killed: list = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append(pid))
    txt = _tool(pkg, "mp_cleanup")()  # default dry_run=True
    assert "dry_run=True" in txt
    assert "would SIGTERM" in txt
    assert "2001" in txt
    assert killed == []


def test_mp_cleanup_reports_nothing_to_do(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import process_health
    monkeypatch.setattr(process_health, "scan", lambda: [
        {"pid": 3001, "ppid": os.getpid(), "rss_kb": 200_000, "etime": "5:00",
         "command": "threadkeeper.server", "parent_alive": True,
         "heartbeat_age_s": 5, "is_self": False, "is_orphaned": False,
         "orphan_reason": "parent_alive"},
    ])
    txt = _tool(pkg, "mp_cleanup")()
    assert txt.startswith("nothing_to_do")
    assert "1 mp process" in txt
