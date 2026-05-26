"""Memory guard for thread-keeper server RSS thresholds."""
from __future__ import annotations

import os
import signal as _sig


_FAKE_CID = "55556666-7777-8888-9999-000011112222"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _proc(pid, rss_mb, *, ppid=None):
    return {
        "pid": pid,
        "ppid": os.getpid() if ppid is None else ppid,
        "rss_kb": rss_mb * 1024,
        "rss_mb": rss_mb,
        "etime": "1:00",
        "command": "python -m threadkeeper.server",
        "parent_alive": True,
        "heartbeat_age_s": 5,
        "is_self": pid == os.getpid(),
        "is_orphaned": False,
        "orphan_reason": "parent_alive",
    }


def test_scan_over_limit_splits_warn_and_kill(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 1000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(process_health, "scan", lambda: [
        _proc(1001, 800),
        _proc(1002, 1200),
        _proc(1003, 2500),
    ])

    out = memory_guard.scan_over_limit()
    assert [p["pid"] for p in out["warn"]] == [1002]
    assert [p["pid"] for p in out["kill"]] == [1003]


def test_check_dry_run_does_not_kill(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 1000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(process_health, "scan", lambda: [_proc(1003, 2500)])
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "os.kill",
        lambda pid, sig: killed.append((pid, sig)) if sig != 0 else None,
    )

    out = memory_guard.check_once(dry_run=True, notify=False)
    assert [p["pid"] for p in out["kill"]] == [1003]
    assert out["killed"] == []
    assert killed == []


def test_check_apply_sends_sigterm(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 1000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(process_health, "scan", lambda: [_proc(1004, 2500)])
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr("os.kill", lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(memory_guard, "reclaim_memory", lambda reason="": {
        "before_mb": 2500, "after_mb": 2400, "freed_mb": 100,
        "pid": os.getpid(), "actions": [],
    })

    out = memory_guard.check_once(dry_run=False, notify=False)
    assert out["killed"] == [1004]
    assert calls == [(1004, _sig.SIGTERM)]


def test_memory_guard_status_tool_reports_thresholds(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_POLL_S", 30)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 1000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(process_health, "scan", lambda: [
        _proc(1001, 800),
        _proc(1002, 1200),
        _proc(1003, 2500),
    ])

    txt = _tool(pkg, "memory_guard_status")()
    assert "state=active" in txt
    assert "warn_mb=1000" in txt
    assert "kill_mb=2000" in txt
    assert "ok pid=1001" in txt
    assert "WARN pid=1002" in txt
    assert "KILL pid=1003" in txt


def test_memory_guard_check_tool_defaults_to_dry_run(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 1000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(process_health, "scan", lambda: [_proc(1005, 2500)])
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "os.kill",
        lambda pid, sig: killed.append((pid, sig)) if sig != 0 else None,
    )

    txt = _tool(pkg, "memory_guard_check")()
    assert txt.startswith("dry_run")
    assert "would SIGTERM pid=1005" in txt
    assert killed == []


def test_scan_reports_aggregate_pressure(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 5000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 6000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 3000)
    monkeypatch.setattr(process_health, "scan", lambda: [
        _proc(1001, 800),
        _proc(1002, 900),
        _proc(1003, 1200),
    ])

    out = memory_guard.scan_over_limit()
    assert out["aggregate"]["rss_mb"] == 2900
    assert out["aggregate"]["warn"] is True
    assert out["aggregate"]["kill"] is False
    assert out["warn"] == []
    assert out["kill"] == []


def test_check_apply_requests_peer_trim_on_aggregate_warn(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 5000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 6000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 0)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_TARGET_SERVERS", 1)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_RETIRE_IDLE_S", 900)
    monkeypatch.setattr(memory_guard, "reclaim_memory", lambda reason="": {
        "before_mb": 900, "after_mb": 800, "freed_mb": 100,
        "pid": os.getpid(), "actions": ["fake"],
    })
    monkeypatch.setattr(process_health, "scan", lambda: [
        _proc(os.getpid(), 900),
        _proc(1002, 1200, ppid=os.getpid()),
    ])

    out = memory_guard.check_once(dry_run=False, notify=False)
    assert sorted(out["reclaim_requests"]["requested"]) == sorted([os.getpid(), 1002])

    conn = pkg["db"].get_db()
    rows = conn.execute(
        "SELECT target_pid FROM resource_controls WHERE action='trim'"
    ).fetchall()
    assert sorted(r["target_pid"] for r in rows) == sorted([os.getpid(), 1002])


def test_check_apply_retires_idle_candidate_on_aggregate_pressure(mp_with_cid, monkeypatch):
    mp_with_cid(_FAKE_CID)
    from threadkeeper import memory_guard, process_health

    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_WARN_MB", 5000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_KILL_MB", 6000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_WARN_MB", 2000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_AGG_KILL_MB", 3000)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_TARGET_SERVERS", 1)
    monkeypatch.setattr(memory_guard, "MEMORY_GUARD_RETIRE_IDLE_S", 900)
    monkeypatch.setattr(memory_guard, "reclaim_memory", lambda reason="": {
        "before_mb": 900, "after_mb": 800, "freed_mb": 100,
        "pid": os.getpid(), "actions": ["fake"],
    })

    def scan():
        return [
            _proc(os.getpid(), 900),
            _proc(1001, 1200, ppid=os.getpid()) | {"heartbeat_age_s": None},
            _proc(1002, 800, ppid=os.getpid()) | {"heartbeat_age_s": 5},
        ]

    monkeypatch.setattr(process_health, "scan", scan)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "os.kill",
        lambda pid, sig: calls.append((pid, sig)) if sig != 0 else None,
    )

    out = memory_guard.check_once(dry_run=False, notify=False)
    assert out["retired"] == [1001]
    assert calls == [(1001, _sig.SIGTERM)]
